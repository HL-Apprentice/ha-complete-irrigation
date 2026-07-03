"""v1.30 restart fail-over — session record/skip hooks in services.py.

The adversarial gate found (and we fixed) a CRITICAL: a MANUAL chunked run's
blocks carry no schedule name, so the old "(block i/n)" regex couldn't tell them
from a fresh logical run -> the parent session got overwritten + cleared after
block 1. The fix is a block-subcall MARKER set by _dispatch_run_blocks (manual AND
scheduled). These tests lock that in. Needs HA importable (services imports HA).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

pytest.importorskip("homeassistant.const")

from custom_components.complete_irrigation import services

# ── _session_decision_for_run: record-vs-skip + final-activation ─────


def test_no_marker_is_logical_run_that_records():
    assert services._session_decision_for_run({}, "switch.z") == (False, True)


def test_intermediate_block_skips_record_and_isnt_final():
    entry = {"block_subcall": {"switch.z": {"idx": 1, "n": 5}}}
    assert services._session_decision_for_run(entry, "switch.z") == (True, False)
    assert "switch.z" not in entry["block_subcall"]  # marker consumed


def test_final_block_skips_record_but_is_final():
    entry = {"block_subcall": {"switch.z": {"idx": 5, "n": 5}}}
    assert services._session_decision_for_run(entry, "switch.z") == (True, True)


# ── _record_active_session / _clear_active_session ──────────────────


def test_record_session_deadline_includes_gap_allowance():
    coord = MagicMock()
    coord.config = {}
    started = datetime(2026, 6, 25, 9, 0, 0, tzinfo=UTC)
    services._record_active_session(
        MagicMock(),
        coord,
        zone="switch.citrus",
        total_minutes=240,
        started_at=started,
        meta={"schedule_id": "s1", "schedule_name": "Citrus"},
        source="scheduled",
        extra_seconds=120,  # 4 inter-block gaps of 30s
    )
    sess = coord.config["active_run_sessions"]["switch.citrus"]
    assert sess["total_minutes"] == 240  # water minutes (resume cap)
    # deadline is gap-inclusive; water_deadline is gaps-free (resume math).
    assert sess["deadline"] == (started + timedelta(minutes=240, seconds=120)).isoformat()
    assert sess["water_deadline"] == (started + timedelta(minutes=240)).isoformat()


def test_clear_session_removes_it():
    coord = MagicMock()
    coord.config = {"active_run_sessions": {"switch.z": {"deadline": "x"}}}
    services._clear_active_session(MagicMock(), coord, "switch.z")
    assert "switch.z" not in coord.config["active_run_sessions"]


# ── _dispatch_run_blocks flags block sub-calls (the critical fix) ────


def _hass_no_op():
    hass = MagicMock()
    hass.async_create_task = MagicMock()
    hass.services.async_call.side_effect = lambda *a, **k: MagicMock()
    return hass


def test_manual_chunked_run_flags_block_subcall(monkeypatch):
    # A MANUAL chunked run (base_meta=None) must STILL mark block sub-calls, or
    # block 1 would overwrite + clear the parent session (the critical bug).
    monkeypatch.setattr(services, "async_call_later", lambda _h, _d, _cb: MagicMock())
    entry: dict = {}
    services._dispatch_run_blocks(_hass_no_op(), "switch.citrus", entry, [58, 58, 58], 30, None)
    assert entry["block_subcall"]["switch.citrus"] == {"idx": 1, "n": 3}
    # And block 1 would correctly be treated as a non-final block sub-call.
    assert services._session_decision_for_run(entry, "switch.citrus") == (True, False)


def test_scheduled_chunked_run_flags_block_subcall(monkeypatch):
    monkeypatch.setattr(services, "async_call_later", lambda _h, _d, _cb: MagicMock())
    entry: dict = {}
    base_meta = {"schedule_id": "s1", "schedule_name": "Trees"}
    services._dispatch_run_blocks(_hass_no_op(), "switch.trees", entry, [58, 58], 30, base_meta)
    assert entry["block_subcall"]["switch.trees"] == {"idx": 1, "n": 2}


def test_logical_run_supersede_cancels_in_flight_chunk_blocks(monkeypatch):
    # The HIGH gate fix: a single (non-block) run started over an in-flight chunked
    # run must cancel that chunk's pending block timers, so orphaned blocks can't
    # re-open the valve untracked after the shared session is cleared.
    cancels = []

    def _fake_acl(_h, _d, _cb):
        c = MagicMock()
        cancels.append(c)
        return c

    monkeypatch.setattr(services, "async_call_later", _fake_acl)
    entry: dict = {}
    services._dispatch_run_blocks(_hass_no_op(), "switch.z", entry, [58, 58, 58], 30, None)
    assert len(entry["block_timers"]["switch.z"]) == 2  # blocks 2 & 3 pending

    # block 1's run_zone consumes block-1's marker (a block sub-call, not final):
    assert services._session_decision_for_run(entry, "switch.z") == (True, False)
    # Now a fresh LOGICAL run arrives (no marker) — not a block sub-call ...
    assert services._session_decision_for_run(entry, "switch.z") == (False, True)
    # ... so handle_run_zone runs the supersede: cancel the chunk's pending blocks.
    services._cancel_block_timers(entry, "switch.z")
    assert all(c.called for c in cancels)
    assert "switch.z" not in entry.get("block_timers", {})
