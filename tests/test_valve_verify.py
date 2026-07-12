"""v1.40 — "check back" valve-actuation verification.

After the integration commands a zone switch on or off, a one-shot check at
+verify_switch_seconds re-reads the switch state and reacts if the command
didn't take (Rachio-cloud/WiFi command loss): retry once, then alert + abort
(ON side) or alert "stuck open" (OFF side).

Covered here:
  • HassJob classification — the verify closures must NOT be Executor jobs
    (the v1.38.1 bug class: plain closures handed to async_call_later run in
    a worker thread where hass APIs raise). Same lock as test_block_history.
  • ON verify: already-on quiet pass / retry-recovers / retry-fails-abort
    (cleanup mirrors _auto_stop, incl. is_final_activation session semantics)
    / externally-detected off defers to the debounce machinery / run already
    ended / disabled at 0.
  • OFF verify: closed quiet pass / retry-recovers / retry-fails stuck-open
    alert / superseded by a new run (next chunked block) / disabled at 0.
  • Timer lifecycle: _cancel_handles + _cleanup_entry_handles disarm pending
    verifies (no leaked timers, no verify after a legitimate stop).
  • Chunking: _dispatch_run_blocks arms NO verify itself — each block's own
    run_zone sub-call arms exactly one.
  • Config plumbing: verify_switch_seconds in BOTH the schema and the
    persistence table (past bug: schema key shipped but never persisted).

Fake-hass harness style follows test_block_cancellation.py; these need HA
importable and skip on a bare dev machine.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

# HA-coupled test: skip gracefully in a bare env (CI runs pytest without HA /
# voluptuous installed and relies on importorskip). A bare `import voluptuous`
# here crashed CI collection — match the convention in test_services_schemas.py.
vol = pytest.importorskip("voluptuous")
pytest.importorskip("homeassistant.const")

from homeassistant.core import HassJob, HassJobType  # noqa: E402

from custom_components.complete_irrigation import services  # noqa: E402
from custom_components.complete_irrigation.manual_run import (  # noqa: E402
    ManualRun,
    ManualRunTracker,
)
from custom_components.complete_irrigation.run_guard import (  # noqa: E402
    DEFAULT_VERIFY_SWITCH_SECONDS,
    valve_verify_off_decision,
    valve_verify_on_decision,
)

ZONE = "switch.front_drip"
FRIENDLY = "Front Drip"


# ── Harness ────────────────────────────────────────────────────────


def _patch_timers(monkeypatch):
    """Capture scheduled (delay, callback) pairs and hand back cancel mocks."""
    captured: list[tuple] = []
    cancels: list[MagicMock] = []

    def _fake_acl(_hass, delay, cb):
        captured.append((delay, cb))
        c = MagicMock()
        cancels.append(c)
        return c

    monkeypatch.setattr(services, "async_call_later", _fake_acl)
    return captured, cancels


def _make_hass():
    """Fake hass with an awaitable service registry + a mutable zone state."""
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock()
    zone_states: dict[str, MagicMock] = {}

    def set_state(state: str) -> None:
        s = MagicMock()
        s.state = state
        zone_states[ZONE] = s

    hass.states.get.side_effect = lambda eid: zone_states.get(eid)
    return hass, set_state


def _make_coord(verify_seconds: int = 30):
    coord = MagicMock()
    coord.config = {
        "verify_switch_seconds": verify_seconds,
        "active_run_sessions": {ZONE: {"total_minutes": 10}},
    }
    coord.notifier.notify = AsyncMock()
    coord.run_history.abort_run = MagicMock(return_value=MagicMock())
    coord.async_save_run_history = AsyncMock()
    # Handed to hass.async_create_task (a MagicMock) by _clear_active_session,
    # so it must NOT produce a real coroutine (filterwarnings=error).
    coord.async_save_config = MagicMock()
    return coord


def _make_entry_data(coord) -> dict:
    return {
        "manual_runs": ManualRunTracker(),
        "cancel_handles": {},
        "coordinator": coord,
    }


def _start_run(entry_data) -> None:
    entry_data["manual_runs"].start(
        ManualRun(entity_id=ZONE, start_at=datetime.now(UTC), duration=timedelta(minutes=10))
    )


# ── The load-bearing regression: verify closures run ON THE EVENT LOOP ──


def test_verify_callbacks_are_not_executor_jobs(monkeypatch):
    """HassJob must classify both verify closures as Coroutinefunction.

    A plain (sync, un-decorated) closure is HassJobType.Executor -> worker
    thread -> hass state/task APIs raise. Exact v1.38.1 bug class."""
    captured, _cancels = _patch_timers(monkeypatch)
    hass, _set_state = _make_hass()
    coord = _make_coord()
    entry_data = _make_entry_data(coord)

    services._schedule_valve_verify_on(hass, entry_data, ZONE, coord, FRIENDLY, True)
    services._schedule_valve_verify_off(hass, entry_data, ZONE, coord, FRIENDLY)

    assert len(captured) == 2
    for _delay, cb in captured:
        job = HassJob(cb)
        assert job.job_type is HassJobType.Coroutinefunction
        assert job.job_type is not HassJobType.Executor


# ── ON verify ──────────────────────────────────────────────────────


async def test_on_verify_already_on_is_quiet(monkeypatch):
    captured, _cancels = _patch_timers(monkeypatch)
    hass, set_state = _make_hass()
    coord = _make_coord()
    entry_data = _make_entry_data(coord)
    _start_run(entry_data)
    set_state("on")

    services._schedule_valve_verify_on(hass, entry_data, ZONE, coord, FRIENDLY, True)
    assert captured[0][0] == 30  # armed at +verify_switch_seconds
    await captured[0][1]()

    hass.services.async_call.assert_not_called()  # no retry
    coord.notifier.notify.assert_not_called()
    coord.run_history.abort_run.assert_not_called()
    assert entry_data["manual_runs"].get(ZONE) is not None  # run continues
    assert entry_data["verify_timers"] == {}  # handle self-cleaned
    assert len(captured) == 1  # no second check armed


async def test_on_verify_retry_recovers(monkeypatch):
    captured, cancels = _patch_timers(monkeypatch)
    hass, set_state = _make_hass()
    coord = _make_coord()
    entry_data = _make_entry_data(coord)
    _start_run(entry_data)
    set_state("off")  # the turn-on command was lost — state never flipped

    services._schedule_valve_verify_on(hass, entry_data, ZONE, coord, FRIENDLY, True)
    await captured[0][1]()

    # One blocking re-assert + a second check-back armed.
    hass.services.async_call.assert_awaited_once_with(
        "switch", "turn_on", {"entity_id": ZONE}, blocking=True
    )
    assert len(captured) == 2
    assert entry_data["verify_timers"][ZONE] is cancels[1]

    # The retry took — the second check passes quietly.
    set_state("on")
    await captured[1][1]()
    coord.notifier.notify.assert_not_called()
    coord.run_history.abort_run.assert_not_called()
    assert entry_data["manual_runs"].get(ZONE) is not None


async def test_on_verify_retry_fails_aborts_and_alerts(monkeypatch):
    captured, _cancels = _patch_timers(monkeypatch)
    hass, set_state = _make_hass()
    coord = _make_coord()
    entry_data = _make_entry_data(coord)
    _start_run(entry_data)
    auto_stop_cancel, listener_cancel = MagicMock(), MagicMock()
    entry_data["cancel_handles"][ZONE] = (auto_stop_cancel, listener_cancel)
    set_state("off")

    services._schedule_valve_verify_on(hass, entry_data, ZONE, coord, FRIENDLY, True)
    await captured[0][1]()  # attempt 1 — retries + re-arms
    await captured[1][1]()  # attempt 2 — still off: abort

    # Cleanup mirrors _auto_stop: tracker stopped, handles cancelled.
    assert entry_data["manual_runs"].get(ZONE) is None
    auto_stop_cancel.assert_called_once()
    listener_cancel.assert_called_once()
    # Run history: aborted with the exact reason.
    kwargs = coord.run_history.abort_run.call_args.kwargs
    assert kwargs["reason"] == "valve failed to actuate"
    coord.async_save_run_history.assert_awaited_once()
    # Critical notification naming the zone + the water risk.
    notify_args, notify_kwargs = coord.notifier.notify.call_args
    assert FRIENDLY in notify_args[0]
    assert "may NOT be flowing" in notify_args[0]
    assert notify_kwargs["category"] == "critical"
    assert notify_kwargs["event_type"] == "valve_verify_failed"
    # Final activation -> restart-resume session cleared (like _auto_stop).
    assert ZONE not in coord.config["active_run_sessions"]
    # Defensive off commanded — nothing else remains to stop a stale-on valve.
    off_calls = [c for c in hass.services.async_call.await_args_list if c.args[1] == "turn_off"]
    assert len(off_calls) == 1
    assert entry_data["verify_timers"] == {}  # nothing left armed


async def test_on_verify_fail_keeps_session_for_non_final_block(monkeypatch):
    """An intermediate chunked block keeps the restart-resume session on
    abort — same is_final_activation semantics as _auto_stop."""
    captured, _cancels = _patch_timers(monkeypatch)
    hass, set_state = _make_hass()
    coord = _make_coord()
    entry_data = _make_entry_data(coord)
    _start_run(entry_data)
    set_state("off")

    services._schedule_valve_verify_on(
        hass,
        entry_data,
        ZONE,
        coord,
        FRIENDLY,
        False,  # block i < n
    )
    await captured[0][1]()
    await captured[1][1]()

    assert coord.run_history.abort_run.called
    assert ZONE in coord.config["active_run_sessions"]  # session survives


async def test_on_verify_defers_to_external_off_debounce(monkeypatch):
    """An off DETECTED by the state listener mid-window means the switch DID
    come on and something (possibly the user) turned it off — the external-off
    machinery owns that path. The verify must not retry over it."""
    captured, _cancels = _patch_timers(monkeypatch)
    hass, set_state = _make_hass()
    coord = _make_coord()
    entry_data = _make_entry_data(coord)
    _start_run(entry_data)
    set_state("off")
    entry_data["external_off"] = {ZONE: {"pending": MagicMock(), "reasserts_left": 2}}

    services._schedule_valve_verify_on(hass, entry_data, ZONE, coord, FRIENDLY, True)
    await captured[0][1]()

    hass.services.async_call.assert_not_called()  # no turn_on fight
    coord.notifier.notify.assert_not_called()
    coord.run_history.abort_run.assert_not_called()
    assert entry_data["manual_runs"].get(ZONE) is not None
    assert len(captured) == 1  # no second check armed


async def test_on_verify_noop_after_run_already_ended(monkeypatch):
    captured, _cancels = _patch_timers(monkeypatch)
    hass, set_state = _make_hass()
    coord = _make_coord()
    entry_data = _make_entry_data(coord)
    _start_run(entry_data)
    set_state("off")

    services._schedule_valve_verify_on(hass, entry_data, ZONE, coord, FRIENDLY, True)
    entry_data["manual_runs"].stop(ZONE)  # run ended while the check waited
    await captured[0][1]()

    hass.services.async_call.assert_not_called()
    coord.notifier.notify.assert_not_called()
    assert len(captured) == 1


# ── OFF verify ─────────────────────────────────────────────────────


async def test_off_verify_closed_is_quiet(monkeypatch):
    captured, _cancels = _patch_timers(monkeypatch)
    hass, set_state = _make_hass()
    coord = _make_coord()
    entry_data = _make_entry_data(coord)  # run already ended — tracker empty
    set_state("off")

    services._schedule_valve_verify_off(hass, entry_data, ZONE, coord, FRIENDLY)
    await captured[0][1]()

    hass.services.async_call.assert_not_called()
    coord.notifier.notify.assert_not_called()
    assert entry_data["verify_timers"] == {}
    assert len(captured) == 1


async def test_off_verify_retry_recovers(monkeypatch):
    captured, _cancels = _patch_timers(monkeypatch)
    hass, set_state = _make_hass()
    coord = _make_coord()
    entry_data = _make_entry_data(coord)
    set_state("on")  # commanded off, still reporting on

    services._schedule_valve_verify_off(hass, entry_data, ZONE, coord, FRIENDLY)
    await captured[0][1]()

    hass.services.async_call.assert_awaited_once_with(
        "switch", "turn_off", {"entity_id": ZONE}, blocking=True
    )
    assert len(captured) == 2

    set_state("off")  # the retry closed it
    await captured[1][1]()
    coord.notifier.notify.assert_not_called()


async def test_off_verify_stuck_open_retries_then_alerts(monkeypatch):
    captured, _cancels = _patch_timers(monkeypatch)
    hass, set_state = _make_hass()
    coord = _make_coord()
    entry_data = _make_entry_data(coord)
    set_state("on")

    services._schedule_valve_verify_off(hass, entry_data, ZONE, coord, FRIENDLY)
    await captured[0][1]()  # attempt 1 — retry turn_off + re-arm
    await captured[1][1]()  # attempt 2 — still on: alert

    notify_args, notify_kwargs = coord.notifier.notify.call_args
    assert FRIENDLY in notify_args[0]
    assert "STUCK OPEN" in notify_args[0]
    assert notify_kwargs["category"] == "critical"
    assert notify_kwargs["event_type"] == "valve_stuck_open"
    # The run is already closed in history — the OFF side never touches it.
    coord.run_history.abort_run.assert_not_called()
    assert entry_data["verify_timers"] == {}


async def test_off_verify_superseded_by_new_run(monkeypatch):
    """A new run inside the window (e.g. the NEXT chunked block, gap ~= verify
    window) owns the valve — the old off-verify must not turn it off."""
    captured, _cancels = _patch_timers(monkeypatch)
    hass, set_state = _make_hass()
    coord = _make_coord()
    entry_data = _make_entry_data(coord)
    set_state("on")

    services._schedule_valve_verify_off(hass, entry_data, ZONE, coord, FRIENDLY)
    _start_run(entry_data)  # next block / fresh run started meanwhile
    await captured[0][1]()

    hass.services.async_call.assert_not_called()  # no turn_off fight
    coord.notifier.notify.assert_not_called()
    assert len(captured) == 1


# ── Disabled + timer lifecycle ─────────────────────────────────────


def test_verify_disabled_when_zero(monkeypatch):
    captured, _cancels = _patch_timers(monkeypatch)
    hass, _set_state = _make_hass()
    coord = _make_coord(verify_seconds=0)
    entry_data = _make_entry_data(coord)

    services._schedule_valve_verify_on(hass, entry_data, ZONE, coord, FRIENDLY, True)
    services._schedule_valve_verify_off(hass, entry_data, ZONE, coord, FRIENDLY)

    assert captured == []  # nothing armed
    assert "verify_timers" not in entry_data


def test_verify_seconds_default_and_bad_values():
    coord = MagicMock()
    coord.config = {}
    assert services._verify_seconds(coord) == DEFAULT_VERIFY_SWITCH_SECONDS
    assert services._verify_seconds(None) == DEFAULT_VERIFY_SWITCH_SECONDS
    coord.config = {"verify_switch_seconds": 0}
    assert services._verify_seconds(coord) == 0
    coord.config = {"verify_switch_seconds": "garbage"}
    assert services._verify_seconds(coord) == DEFAULT_VERIFY_SWITCH_SECONDS


def test_cancel_handles_disarms_pending_verify_even_without_pair(monkeypatch):
    """A post-run OFF-verify has no cancel_handles pair left — the disarm must
    happen BEFORE _cancel_handles's early return, or a new run on the zone
    would leave the stale verify armed against it."""
    _captured, cancels = _patch_timers(monkeypatch)
    hass, _set_state = _make_hass()
    coord = _make_coord()
    entry_data = _make_entry_data(coord)

    services._schedule_valve_verify_off(hass, entry_data, ZONE, coord, FRIENDLY)
    assert entry_data["cancel_handles"] == {}  # no pair — early-return path

    services._cancel_handles(entry_data, ZONE)
    cancels[0].assert_called_once()
    assert entry_data["verify_timers"] == {}


def test_cleanup_entry_handles_disarms_verify_timers(monkeypatch):
    _captured, cancels = _patch_timers(monkeypatch)
    hass, _set_state = _make_hass()
    coord = _make_coord()
    entry_data = _make_entry_data(coord)
    _start_run(entry_data)

    services._schedule_valve_verify_on(hass, entry_data, ZONE, coord, FRIENDLY, True)
    services._cleanup_entry_handles(entry_data)

    cancels[0].assert_called_once()
    assert entry_data["verify_timers"] == {}


# ── Chunking: dispatch arms no verify of its own ───────────────────


def test_chunk_dispatch_arms_no_verify(monkeypatch):
    """The >cap dispatch branch never arms a verify — each block re-enters the
    single-activation path via its own run_zone sub-call and arms exactly one
    there (no double-verify per block)."""
    captured, _cancels = _patch_timers(monkeypatch)
    hass = MagicMock()
    hass.services.async_call = MagicMock(return_value=MagicMock())  # sync — no coroutine
    hass.async_create_task = MagicMock()
    entry_data: dict = {}

    services._dispatch_run_blocks(hass, ZONE, entry_data, [58, 58, 10], 30, None)

    assert "verify_timers" not in entry_data
    # Everything scheduled here is a block timer (sync @callback), never one
    # of the async verify closures.
    for _delay, cb in captured:
        assert HassJob(cb).job_type is HassJobType.Callback


# ── Pure decision logic (run_guard.py) ─────────────────────────────


def test_valve_verify_on_decision_matrix():
    dec = valve_verify_on_decision
    assert (
        dec(run_live=False, state_now="off", external_off_pending=False, first_attempt=True)
        == "ended"
    )
    assert (
        dec(run_live=True, state_now="on", external_off_pending=False, first_attempt=True)
        == "verified"
    )
    assert (
        dec(run_live=True, state_now="on", external_off_pending=True, first_attempt=False)
        == "verified"
    )
    assert (
        dec(run_live=True, state_now="off", external_off_pending=True, first_attempt=True)
        == "defer_external"
    )
    assert (
        dec(run_live=True, state_now="off", external_off_pending=False, first_attempt=True)
        == "retry"
    )
    assert (
        dec(run_live=True, state_now="unavailable", external_off_pending=False, first_attempt=True)
        == "retry"
    )
    assert (
        dec(run_live=True, state_now="off", external_off_pending=False, first_attempt=False)
        == "fail"
    )


def test_valve_verify_off_decision_matrix():
    dec = valve_verify_off_decision
    assert dec(new_run_live=True, state_now="on", first_attempt=True) == "superseded"
    assert dec(new_run_live=False, state_now="off", first_attempt=True) == "verified"
    assert dec(new_run_live=False, state_now="unavailable", first_attempt=True) == "verified"
    assert dec(new_run_live=False, state_now="on", first_attempt=True) == "retry"
    assert dec(new_run_live=False, state_now="on", first_attempt=False) == "fail"


# ── Config plumbing ────────────────────────────────────────────────


def test_general_config_schema_accepts_verify_switch_seconds():
    schema = services._SET_GENERAL_CONFIG_SCHEMA
    assert schema({"verify_switch_seconds": 0})["verify_switch_seconds"] == 0
    assert schema({"verify_switch_seconds": 300})["verify_switch_seconds"] == 300
    assert schema({"verify_switch_seconds": "45"})["verify_switch_seconds"] == 45
    with pytest.raises(vol.Invalid):
        schema({"verify_switch_seconds": 301})
    with pytest.raises(vol.Invalid):
        schema({"verify_switch_seconds": -1})


def test_every_general_config_schema_key_is_persisted():
    """Past bug: a schema key shipped with no _GENERAL_CONFIG_FIELDS entry, so
    set_general_config validated it and then silently never stored it. Lock
    the two tables together so that can't recur."""
    schema_keys = {str(k) for k in services._SET_GENERAL_CONFIG_SCHEMA.schema}
    assert schema_keys == set(services._GENERAL_CONFIG_FIELDS)
    # And the new key's transform round-trips an int.
    assert services._GENERAL_CONFIG_FIELDS["verify_switch_seconds"](45) == 45
