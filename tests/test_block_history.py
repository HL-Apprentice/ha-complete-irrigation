"""v1.38.1 — chunked-run blocks 2..n must reach run history.

Root cause locked in here: _dispatch_run_blocks scheduled blocks 2..n with
async_call_later handing it a PLAIN closure. HassJob classifies a plain
function as HassJobType.Executor, so blocks 2+ fired in a worker THREAD where
hass.async_create_task raises RuntimeError (HA's thread-safety guard, ERROR
behavior for custom integrations since ~2024.5). The run_zone sub-call was
never made -> zero "(block 2+/n)" history records, ever (only "(block 1/n)"
existed, because block 1 fires synchronously inside handle_run_zone on the
event loop). Fix: the block-fire closure is now an @callback so it runs on
the event loop. Same-class fixes applied to coordinator.py's post-install /
restart-resume / resume-retry lambdas.

These tests need HA importable (like test_block_cancellation); the record
lifecycle test is pure run_history logic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

pytest.importorskip("homeassistant.const")

from homeassistant.core import HassJob, HassJobType

from custom_components.complete_irrigation import services
from custom_components.complete_irrigation.run_history import (
    STATUS_COMPLETED,
    RunHistoryStore,
)

ZONE = "switch.back_grass_blue"


def _make_hass(dispatch_log):
    """Fake hass whose run_zone 'dispatch' logs the pending block label at the
    moment the sub-call is made (that's the label handle_run_zone would pop)."""
    hass = MagicMock()
    entry_data: dict = {}

    def _async_call(domain, service, data, blocking=False):
        meta = entry_data.get("pending_run_meta", {}).get(data["entity_id"], {})
        marker = entry_data.get("block_subcall", {}).get(data["entity_id"], {})
        dispatch_log.append(
            {
                "minutes": data["minutes"],
                "schedule_name": meta.get("schedule_name"),
                "marker": dict(marker),
            }
        )
        return MagicMock()  # sync — not a coroutine (filterwarnings=error)

    hass.services.async_call.side_effect = _async_call
    hass.async_create_task = MagicMock()
    return hass, entry_data


def _patch_timers(monkeypatch):
    captured: list[tuple] = []

    def _fake_acl(_hass, delay, cb):
        captured.append((delay, cb))
        return MagicMock()

    monkeypatch.setattr(services, "async_call_later", _fake_acl)
    return captured


# ── The load-bearing regression: block timers must run ON THE EVENT LOOP ──


def test_block_timer_action_is_event_loop_callback(monkeypatch):
    """HassJob must classify the scheduled block-fire closure as Callback.

    Without @callback it is HassJobType.Executor -> worker thread ->
    hass.async_create_task raises -> blocks 2+ never dispatch run_zone and
    never appear in run history. This is the exact regression that shipped."""
    captured = _patch_timers(monkeypatch)
    hass, entry_data = _make_hass([])

    services._dispatch_run_blocks(hass, ZONE, entry_data, [58, 58, 10], 30, None)

    assert len(captured) == 2  # blocks 2 and 3 scheduled
    for _delay, cb in captured:
        assert HassJob(cb).job_type is HassJobType.Callback


# ── Every block dispatches run_zone carrying its own "(block i/n)" label ──


def test_every_block_dispatches_with_its_own_label(monkeypatch):
    captured = _patch_timers(monkeypatch)
    dispatches: list[dict] = []
    hass, entry_data = _make_hass(dispatches)
    base_meta = {"schedule_name": "Back Grass", "requested_minutes": 126}

    services._dispatch_run_blocks(hass, ZONE, entry_data, [58, 58, 10], 30, base_meta)
    # Simulate the two timers elapsing (block 1 fired synchronously already).
    for _delay, cb in captured:
        cb()

    assert [d["minutes"] for d in dispatches] == [58, 58, 10]
    assert [d["schedule_name"] for d in dispatches] == [
        "Back Grass (block 1/3)",
        "Back Grass (block 2/3)",
        "Back Grass (block 3/3)",
    ]
    # Each sub-call carries the marker that guards the parent resume session.
    assert [d["marker"] for d in dispatches] == [
        {"idx": 1, "n": 3},
        {"idx": 2, "n": 3},
        {"idx": 3, "n": 3},
    ]


# ── History lifecycle: 3 sequential blocks -> 3 CLOSED records ──


def test_three_sequential_blocks_yield_three_closed_records():
    """Simulate what handle_run_zone + _auto_stop do per block: start_run at
    block start, complete_run at its auto-stop. The History tab must end up
    with N closed rows for an N-block run — no stale RUNNING record."""
    store = RunHistoryStore()
    t0 = datetime(2026, 7, 10, 14, 0, 0, tzinfo=UTC)
    blocks = [58, 58, 10]
    cursor = t0
    for i, bm in enumerate(blocks, start=1):
        store.start_run(
            zone_entity_id=ZONE,
            zone_name="Back Grass (Blue)",
            requested_minutes=bm,
            source="scheduled",
            started_at=cursor,
            schedule_id="sched-1",
            schedule_name=f"Back Grass (block {i}/3)",
        )
        cursor = cursor + timedelta(minutes=bm)
        store.complete_run(ZONE, ended_at=cursor)
        cursor = cursor + timedelta(seconds=30)  # inter-block gap

    records = store.all()  # newest first
    assert len(records) == 3
    assert [r.schedule_name for r in reversed(records)] == [
        "Back Grass (block 1/3)",
        "Back Grass (block 2/3)",
        "Back Grass (block 3/3)",
    ]
    assert all(r.status == STATUS_COMPLETED for r in records)
    assert [r.actual_minutes for r in reversed(records)] == [58, 58, 10]
    assert store.latest_open(ZONE) is None  # nothing left RUNNING
