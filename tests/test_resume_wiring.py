"""v1.30 restart fail-over — coordinator glue (_resume_interrupted_runs).

The resume DECISION is unit-tested purely in test_session_resume. THESE cover the
coordinator's HA wiring: a resumable session re-fires run_zone for the remaining
minutes (stashing resumed meta); a past-its-end session turns the valve off +
closes the record; no sessions = no-op. Needs HA importable, like the other
coordinator-tier tests.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("homeassistant.util.dt")

from homeassistant.util import dt as dt_util

from custom_components.complete_irrigation.const import DOMAIN
from custom_components.complete_irrigation.coordinator import ScheduleCoordinator


def _coord_with_sessions(sessions) -> ScheduleCoordinator:
    coord = ScheduleCoordinator(MagicMock(), "e1")
    coord._hass.data = {DOMAIN: {"e1": {}}}
    coord.async_save_config = AsyncMock()
    coord.async_save_run_history = AsyncMock()
    coord._config["active_run_sessions"] = sessions

    # Simulate run_zone re-recording the session (real run_zone does this on a
    # successful start) so the coordinator's "did the resume actually start?"
    # check sees the started_at advance. Tests can override this mock.
    async def _call(domain, service, data=None, blocking=False):
        if service == "run_zone" and isinstance(data, dict):
            z = data.get("entity_id")
            sess = (coord._config.get("active_run_sessions") or {}).get(z)
            if sess is not None:
                sess["started_at"] = f"started-{data.get('minutes')}"
        return MagicMock()

    coord._hass.services.async_call = AsyncMock(side_effect=_call)
    return coord


async def test_resume_fires_run_zone_for_remaining_minutes():
    coord = _coord_with_sessions(
        {
            "switch.citrus": {
                # +30s buffer so the now()-to-now() microseconds don't floor 40 -> 39.
                "deadline": (dt_util.now() + timedelta(minutes=40, seconds=30)).isoformat(),
                "total_minutes": 240,
                "schedule_id": "s1",
                "schedule_name": "Citrus",
            }
        }
    )
    await coord._resume_interrupted_runs()
    calls = coord._hass.services.async_call.call_args_list
    run_calls = [c for c in calls if c.args[1] == "run_zone"]
    assert len(run_calls) == 1
    assert run_calls[0].args[2]["entity_id"] == "switch.citrus"
    assert run_calls[0].args[2]["minutes"] == 40  # floor(40.5) — never rounds UP
    # Resumed meta stashed so it records under the schedule, marked resumed.
    meta = coord._hass.data[DOMAIN]["e1"]["pending_run_meta"]["switch.citrus"]
    assert "(resumed)" in meta["schedule_name"]
    assert meta["requested_minutes"] == 40


async def test_close_turns_valve_off_and_drops_session():
    coord = _coord_with_sessions(
        {"switch.bb": {"deadline": (dt_util.now() - timedelta(minutes=10)).isoformat()}}
    )
    await coord._resume_interrupted_runs()
    calls = coord._hass.services.async_call.call_args_list
    off = [c for c in calls if c.args[0] == "switch" and c.args[1] == "turn_off"]
    assert off and off[0].args[2]["entity_id"] == "switch.bb"
    # No run_zone for a past-its-end run; session dropped.
    assert not any(c.args[1] == "run_zone" for c in calls)
    assert "switch.bb" not in coord._config.get("active_run_sessions", {})


async def test_bad_session_fails_safe_to_close():
    coord = _coord_with_sessions({"switch.z": {"deadline": "garbage"}})
    await coord._resume_interrupted_runs()
    calls = coord._hass.services.async_call.call_args_list
    assert any(c.args[1] == "turn_off" for c in calls)
    assert not any(c.args[1] == "run_zone" for c in calls)


async def test_noop_when_no_sessions():
    coord = _coord_with_sessions({})
    await coord._resume_interrupted_runs()
    coord._hass.services.async_call.assert_not_called()


async def test_resume_defers_and_retries_when_switch_unavailable(monkeypatch):
    # The re-gate fix: a slow switch reconnect at resume time must NOT lose the
    # run — keep the session and retry, never fire run_zone into an unavailable
    # switch (which would refuse) and never drop the session.
    coord = _coord_with_sessions(
        {"switch.citrus": {"deadline": (dt_util.now() + timedelta(minutes=40)).isoformat()}}
    )
    unavailable = MagicMock()
    unavailable.state = "unavailable"
    coord._hass.states.get.return_value = unavailable
    scheduled = []
    monkeypatch.setattr(
        "homeassistant.helpers.event.async_call_later",
        lambda *a, **k: scheduled.append(a) or MagicMock(),
    )
    await coord._resume_interrupted_runs()
    calls = coord._hass.services.async_call.call_args_list
    assert not any(c.args[1] == "run_zone" for c in calls)  # didn't fire into a dead switch
    sess = coord._config["active_run_sessions"].get("switch.citrus")
    assert sess is not None  # session KEPT
    assert sess.get("resuming_gated") is True  # marked gated -> UI won't show false "Running"
    assert scheduled and coord._resume_attempts == 1  # a bounded retry was armed


async def test_retry_is_scoped_to_deferred_zones():
    # Multi-model fix: a retry must only re-process the deferred zones, never
    # re-fire a zone already resumed on the first pass.
    coord = _coord_with_sessions(
        {"switch.z": {"deadline": (dt_util.now() + timedelta(minutes=40)).isoformat()}}
    )
    await coord._resume_interrupted_runs(zones={"switch.other"})  # z is out of scope
    coord._hass.services.async_call.assert_not_called()


async def test_resume_honors_stop_during_loop():
    # Gemini's catch: if a session is removed (user Stop) while we await an earlier
    # zone, the loop must NOT resume it from the stale snapshot.
    dl = (dt_util.now() + timedelta(minutes=40, seconds=30)).isoformat()
    coord = _coord_with_sessions({"switch.a": {"deadline": dl}, "switch.b": {"deadline": dl}})
    on = MagicMock()
    on.state = "on"
    coord._hass.states.get.return_value = on

    async def _call(domain, service, data=None, blocking=False):
        if service == "run_zone" and isinstance(data, dict):
            z = data.get("entity_id")
            sess = (coord._config.get("active_run_sessions") or {}).get(z)
            if sess is not None:
                sess["started_at"] = f"started-{data.get('minutes')}"
            if z == "switch.a":  # simulate a Stop on b while we process a
                coord._config["active_run_sessions"].pop("switch.b", None)
        return MagicMock()

    coord._hass.services.async_call = AsyncMock(side_effect=_call)
    await coord._resume_interrupted_runs()
    run_zones = [
        c.args[2]["entity_id"]
        for c in coord._hass.services.async_call.call_args_list
        if c.args[1] == "run_zone"
    ]
    assert "switch.a" in run_zones
    assert "switch.b" not in run_zones  # stopped mid-loop -> not resurrected


async def test_gives_up_cleanly_after_max_retries(monkeypatch):
    # Multi-model fix: out of retries must NOT leave a zombie session (false
    # "Running") — clear it, force the valve off, record an abort.
    from custom_components.complete_irrigation.coordinator import _RESUME_MAX_ATTEMPTS

    coord = _coord_with_sessions(
        {"switch.z": {"deadline": (dt_util.now() + timedelta(minutes=40)).isoformat()}}
    )
    unavailable = MagicMock()
    unavailable.state = "unavailable"
    coord._hass.states.get.return_value = unavailable
    coord._resume_attempts = _RESUME_MAX_ATTEMPTS  # already at the cap
    coord._run_history.abort_run = MagicMock()
    monkeypatch.setattr("homeassistant.helpers.event.async_call_later", lambda *a, **k: MagicMock())
    # zones= (a RETRY) so the initial-pass attempt reset doesn't fire.
    await coord._resume_interrupted_runs(zones={"switch.z"})
    assert "switch.z" not in coord._config.get("active_run_sessions", {})  # no zombie
    calls = coord._hass.services.async_call.call_args_list
    assert any(c.args[0] == "switch" and c.args[1] == "turn_off" for c in calls)  # valve off
    coord._run_history.abort_run.assert_called_once()


# ── v1.39.1 watering-critical fixes ──────────────────────────────────────────


async def test_overlapping_shared_line_sessions_resume_one_per_pass(monkeypatch):
    # Restart during a LIVE gap-inserted run leaves TWO overlapping shared-line
    # sessions. Resuming both concurrently opens two valves on the shared
    # manifold — only ONE may dispatch per pass; the other defers to the retry.
    now = dt_util.now()
    dl = (now + timedelta(minutes=40, seconds=30)).isoformat()
    st_at = (now - timedelta(minutes=10)).isoformat()
    coord = _coord_with_sessions(
        {
            "switch.citrus": {"deadline": dl, "started_at": st_at, "total_minutes": 50},
            "switch.garden": {"deadline": dl, "started_at": st_at, "total_minutes": 50},
        }
    )
    on = MagicMock()
    on.state = "on"
    coord._hass.states.get.return_value = on
    scheduled = []
    monkeypatch.setattr(
        "homeassistant.helpers.event.async_call_later",
        lambda *a, **k: scheduled.append(a) or MagicMock(),
    )
    await coord._resume_interrupted_runs()
    run_zones = [
        c.args[2]["entity_id"]
        for c in coord._hass.services.async_call.call_args_list
        if c.args[1] == "run_zone"
    ]
    assert len(run_zones) == 1, f"expected ONE shared-line resume per pass, got {run_zones}"
    deferred_zone = ({"switch.citrus", "switch.garden"} - set(run_zones)).pop()
    sess = coord._config["active_run_sessions"][deferred_zone]
    assert sess.get("resuming_gated") is True  # deferred (kept + gated), not lost
    assert scheduled and coord._resume_attempts == 1  # retry armed for the deferred zone


async def test_retry_defers_while_the_first_resumed_zone_still_runs(monkeypatch):
    # RETRY pass for the deferred zone: the zone resumed on the first pass now
    # has a FRESH (genuinely live) session outside the retry's ghost set — it
    # must gate the retried zone until it finishes (or retries give up).
    now = dt_util.now()
    coord = _coord_with_sessions(
        {
            "switch.citrus": {  # resumed on the first pass — fresh, live
                "deadline": (now + timedelta(minutes=40)).isoformat(),
                "started_at": now.isoformat(),
                "total_minutes": 50,
            },
            "switch.garden": {  # the deferred ghost being retried
                "deadline": (now + timedelta(minutes=30)).isoformat(),
                "started_at": (now - timedelta(minutes=10)).isoformat(),
                "resuming_gated": True,
                "total_minutes": 40,
            },
        }
    )
    on = MagicMock()
    on.state = "on"
    coord._hass.states.get.return_value = on
    monkeypatch.setattr("homeassistant.helpers.event.async_call_later", lambda *a, **k: MagicMock())
    await coord._resume_interrupted_runs(zones={"switch.garden"})
    run_zones = [
        c for c in coord._hass.services.async_call.call_args_list if c.args[1] == "run_zone"
    ]
    assert run_zones == []  # citrus is genuinely running -> garden stays deferred
    assert coord._config["active_run_sessions"]["switch.garden"].get("resuming_gated") is True


async def test_independent_zone_resumes_are_not_serialized():
    # Independent-supply zones never contend for the shared manifold — both
    # resume in the same pass (the serialization must not over-block them).
    now = dt_util.now()
    dl = (now + timedelta(minutes=40, seconds=30)).isoformat()
    st_at = (now - timedelta(minutes=10)).isoformat()
    coord = _coord_with_sessions(
        {
            "switch.bird_bath": {"deadline": dl, "started_at": st_at, "total_minutes": 50},
            "switch.fountain": {"deadline": dl, "started_at": st_at, "total_minutes": 50},
        }
    )
    coord._config["independent_zones"] = ["switch.bird_bath", "switch.fountain"]
    on = MagicMock()
    on.state = "on"
    coord._hass.states.get.return_value = on
    await coord._resume_interrupted_runs()
    run_zones = [
        c.args[2]["entity_id"]
        for c in coord._hass.services.async_call.call_args_list
        if c.args[1] == "run_zone"
    ]
    assert sorted(run_zones) == ["switch.bird_bath", "switch.fountain"]


# ── v1.39.1: pre-restart ghost block plans are untrusted for gap insertion ──


def test_gap_trusted_sessions_strips_ghost_and_gated_block_plans():
    from custom_components.complete_irrigation.coordinator import ScheduleCoordinator

    coord = ScheduleCoordinator(MagicMock(), "e1")
    now = dt_util.now()
    coord._uptime_started_at = now - timedelta(seconds=90)  # HA started 90s ago
    pre_restart = {  # dispatched by the PREVIOUS process — timers are dead
        "started_at": (now - timedelta(hours=1)).isoformat(),
        "deadline": (now + timedelta(hours=2)).isoformat(),
        "blocks": [58, 58, 58],
        "block_gap_seconds": 1200,
    }
    gated = {  # resume deferred — plan not re-armed yet
        "started_at": (now - timedelta(seconds=30)).isoformat(),
        "deadline": (now + timedelta(hours=2)).isoformat(),
        "blocks": [58, 58],
        "block_gap_seconds": 1200,
        "resuming_gated": True,
    }
    fresh = {  # dispatched THIS uptime — timers armed, plan trusted
        "started_at": (now - timedelta(seconds=30)).isoformat(),
        "deadline": (now + timedelta(hours=2)).isoformat(),
        "blocks": [58, 58],
        "block_gap_seconds": 1200,
    }
    out = coord._gap_trusted_sessions(
        {"switch.a": pre_restart, "switch.b": gated, "switch.c": fresh}
    )
    assert "blocks" not in out["switch.a"] and "block_gap_seconds" not in out["switch.a"]
    assert "blocks" not in out["switch.b"] and "block_gap_seconds" not in out["switch.b"]
    assert out["switch.c"]["blocks"] == [58, 58]  # this-uptime plan kept
    # The ghost still counts as a BUSY span — only its gaps are untrusted.
    assert out["switch.a"]["started_at"] == pre_restart["started_at"]
    assert out["switch.a"]["deadline"] == pre_restart["deadline"]
    # The stored session is never mutated (the filter returns copies).
    assert "blocks" in pre_restart and "blocks" in gated


def test_stale_prerestart_plan_yields_no_insertion_but_still_blocks():
    # The finding's exact scenario: HA restarted mid-chunked-run, resume is
    # gated (switch not back yet), and the stale plan exposes a pin inside the
    # first tick's due window. The pin must NOT be produced from the filtered
    # sessions — but the session must still act as a committed busy span.
    from custom_components.complete_irrigation.conflict_resolver import (
        committed_runs_from_sessions,
    )
    from custom_components.complete_irrigation.coordinator import ScheduleCoordinator
    from custom_components.complete_irrigation.gap_insertion import plan_gap_insertions
    from custom_components.complete_irrigation.run_planner import PlannedRun

    coord = ScheduleCoordinator(MagicMock(), "e1")
    now = dt_util.now()
    coord._uptime_started_at = now - timedelta(seconds=60)
    started = now - timedelta(minutes=60)
    session = {  # 58-min blocks, 20-min gaps; restart landed inside gap 1
        "started_at": started.isoformat(),
        "deadline": (started + timedelta(minutes=174, seconds=2400)).isoformat(),
        "water_deadline": (started + timedelta(minutes=174)).isoformat(),
        "blocks": [58, 58, 58],
        "block_gap_seconds": 1200,
        "resuming_gated": True,
    }
    sessions = {"switch.citrus": session}
    short = PlannedRun(
        zone_entity_id="switch.garden",
        start_at=now - timedelta(minutes=30),  # blocked by the citrus span
        duration_minutes=5,
        schedule_id="garden",
        schedule_name="GARDEN",
    )
    # Pre-fix reachability: fed the RAW session, the planner pins the short
    # into the ghost gap, inside the first tick's catchable window.
    raw_plan = plan_gap_insertions([short], sessions, now)
    assert raw_plan, "scenario must be reachable against the unfiltered session"
    # The fix: the filtered copy exposes no gaps -> no insertion...
    filtered = coord._gap_trusted_sessions(sessions)
    assert plan_gap_insertions([short], filtered, now) == {}
    # ...while the session still blocks the manifold as a committed span.
    assert committed_runs_from_sessions(filtered, now)


# ── v1.39.1: _fire_run belt — a would-chunk gap-inserted run is never fired ──


async def test_fire_run_skips_gap_inserted_run_over_controller_cap():
    from custom_components.complete_irrigation.gap_insertion import REASON_GAP_INSERTED
    from custom_components.complete_irrigation.run_planner import PlannedRun

    coord = _coord_with_sessions({})
    coord._config["controller_max_run_minutes"] = 5  # lowered after planning
    coord._run_history.record_skipped = MagicMock(return_value=MagicMock())
    run = PlannedRun(
        zone_entity_id="switch.garden",
        start_at=dt_util.now(),
        duration_minutes=6,  # > cap -> run_zone would chunk it past its gap
        schedule_id="s1",
        schedule_name="Garden",
        reason=REASON_GAP_INSERTED,
    )
    await coord._fire_run(run)
    calls = coord._hass.services.async_call.call_args_list
    assert not any(c.args[1] == "run_zone" for c in calls)  # never dispatched
    coord._run_history.record_skipped.assert_called_once()
    reason = coord._run_history.record_skipped.call_args.kwargs["reason"]
    assert "controller cap" in reason
