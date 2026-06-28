"""v1.21 — one-zone-at-a-time across LIVE runs.

`committed_runs_from_sessions` turns the active_run_sessions registry (zone ->
session dict, persisted by the run services) into REASON_COMMITTED blockers, and
the resolver routes due scheduled runs AROUND them. A manual run is invisible to
next_runs (it isn't a schedule), so this is the only thing that stops a schedule
firing a second zone into an in-progress manual / scheduled / resumed run.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.complete_irrigation.conflict_resolver import (
    POLICY_DEFER_NEW,
    REASON_COMMITTED,
    committed_runs_from_sessions,
    resolve_conflicts,
)
from custom_components.complete_irrigation.run_planner import PlannedRun, due_runs_since

NOW = datetime(2026, 5, 18, 6, 30, tzinfo=UTC)


def _session(started_min_before, total_minutes, *, sid="citrus", gap_min=0, **extra):
    """Build a session record like services._record_active_session writes."""
    started = NOW - timedelta(minutes=started_min_before)
    deadline = started + timedelta(minutes=total_minutes + gap_min)  # gap-inclusive
    water_deadline = started + timedelta(minutes=total_minutes)  # gap-free
    s = {
        "total_minutes": total_minutes,
        "started_at": started.isoformat(),
        "deadline": deadline.isoformat(),
        "water_deadline": water_deadline.isoformat(),
        "schedule_id": sid if sid else None,
        "schedule_name": (sid or "").upper(),
        "source": "scheduled" if sid else "manual",
    }
    s.update(extra)
    return s


def _scheduled(zone, start_at, dur, sid):
    return PlannedRun(
        zone_entity_id=zone,
        start_at=start_at,
        duration_minutes=dur,
        schedule_id=sid,
        schedule_name=sid.upper(),
    )


# ── committed_runs_from_sessions ────────────────────────────────────


def test_active_session_becomes_committed_blocker():
    sessions = {"switch.citrus": _session(10, 60, sid="citrus")}
    out = committed_runs_from_sessions(sessions, NOW)
    assert len(out) == 1
    r = out[0]
    assert r.zone_entity_id == "switch.citrus"
    assert r.reason == REASON_COMMITTED
    assert r.duration_minutes == 60
    assert r.start_at == NOW - timedelta(minutes=10)
    assert r.schedule_id == "citrus"


def test_gap_inclusive_deadline_used_for_duration():
    # A chunked run (60 water + 5 gap): the busy interval spans the gap too, so a
    # different zone is never slipped into the gap between blocks.
    sessions = {"switch.trees": _session(0, 60, sid="trees", gap_min=5)}
    r = committed_runs_from_sessions(sessions, NOW)[0]
    assert r.duration_minutes == 65


def test_manual_session_has_no_schedule_id():
    sessions = {"switch.front": _session(5, 20, sid=None)}
    r = committed_runs_from_sessions(sessions, NOW)[0]
    assert r.schedule_id == ""
    assert r.reason == REASON_COMMITTED


def test_finished_session_skipped():
    # deadline already passed (well past the buffer) -> not a blocker.
    sessions = {"switch.x": _session(120, 60, sid="x")}  # ended ~60 min ago
    assert committed_runs_from_sessions(sessions, NOW) == []


def test_just_finished_session_blocks_through_buffer_and_catch_margin():
    # Ended 1 min ago — inside the inter-zone buffer + catch margin (2 + 2 = 4 min),
    # so it MUST remain a blocker (a run scheduled right after lands buffer-min later,
    # and a deferred run still fires before the blocker clears).
    just_ended = {"switch.a": _session(31, 30, sid="a")}  # started 31 ago, 30 water
    assert len(committed_runs_from_sessions(just_ended, NOW)) == 1
    # Ended 6 min ago — past buffer + margin -> dropped.
    long_done = {"switch.a": _session(36, 30, sid="a")}
    assert committed_runs_from_sessions(long_done, NOW) == []


def test_deferred_run_not_stranded_when_blocker_expires():
    # The round-2 stranding bug: a run deferred behind a temporary live run must be
    # FIRED before the blocker clears, never snapped back to its past scheduled time
    # and lost. At the critical tick (now = committed end + resolver buffer), the
    # blocker must still be present so the deferred run is pinned + fired there.
    buf = 2  # DEFAULT_BUFFER_MINUTES used by the resolver + this function
    sessions = {"switch.a": _session(30, 30, sid=None)}  # manual run ending at NOW
    b = _scheduled("switch.b", NOW - timedelta(minutes=25), 10, "b")  # B long overdue
    tick_now = NOW + timedelta(minutes=buf)  # B's deferred slot = deadline + buffer
    committed = committed_runs_from_sessions(sessions, tick_now)
    assert len(committed) == 1, "blocker must survive the catch margin"
    resolved = resolve_conflicts([*committed, b], POLICY_DEFER_NEW, compress_floor_pct=100)
    last = tick_now - timedelta(minutes=1)  # TICK_SECONDS = 60
    due = due_runs_since(resolved, last, tick_now)
    assert any(r.schedule_id == "b" for r in due), "B fires, not stranded"


def test_bad_session_is_logged_not_silent(caplog):
    # Fail-open is correct (one bad record must not freeze all watering), but a
    # still-running zone losing its blocker must be VISIBLE, not silent.
    import logging

    bad = {"switch.a": {"started_at": "not-a-date"}}
    with caplog.at_level(logging.WARNING):
        assert committed_runs_from_sessions(bad, NOW) == []
    assert "switch.a" in caplog.text


def test_bad_sessions_fail_safe_skipped():
    naive = NOW.replace(tzinfo=None)
    bad = {
        "switch.a": {"started_at": "not-a-date", "deadline": "also-bad"},
        "switch.b": {"total_minutes": 30},  # missing timestamps
        "switch.c": "junk",
        "": _session(5, 30),  # empty zone key
        "switch.d": {  # tz-naive timestamps -> can't compare safely, skip
            "started_at": naive.isoformat(),
            "deadline": (naive + timedelta(minutes=30)).isoformat(),
        },
    }
    assert committed_runs_from_sessions(bad, NOW) == []


def test_non_dict_input_is_empty():
    assert committed_runs_from_sessions(None, NOW) == []
    assert committed_runs_from_sessions([], NOW) == []
    assert committed_runs_from_sessions("x", NOW) == []


# ── resolver routes scheduled runs around committed ones ────────────


def test_scheduled_run_defers_past_in_progress_manual_run():
    # Manual run on zone A started 10 min ago for 60 -> busy until NOW+50.
    committed = committed_runs_from_sessions({"switch.a": _session(10, 60, sid=None)}, NOW)
    sched = _scheduled("switch.b", NOW, 20, "b")  # zone B due NOW
    resolved = resolve_conflicts([*committed, sched], POLICY_DEFER_NEW, compress_floor_pct=100)
    b = next(r for r in resolved if r.schedule_id == "b")
    # B is pushed to at/after A's end (NOW+50) + buffer, never fires at NOW.
    assert b.start_at >= NOW + timedelta(minutes=50)


def test_committed_run_preserved_unmoved():
    committed = committed_runs_from_sessions({"switch.a": _session(10, 60, sid="a")}, NOW)
    sched = _scheduled("switch.b", NOW, 20, "b")
    resolved = resolve_conflicts([*committed, sched], POLICY_DEFER_NEW, compress_floor_pct=100)
    a = next(r for r in resolved if r.schedule_id == "a")
    assert a.reason == REASON_COMMITTED
    assert a.start_at == NOW - timedelta(minutes=10)  # never moved / compressed
    assert a.duration_minutes == 60


def test_committed_run_in_tick_window_is_not_refired():
    # A manual run started 1 min ago sits INSIDE this tick's (last, now] window —
    # due_runs_since would return it, so the coordinator must filter it out before
    # firing (else the manual run is double-fired).
    committed = committed_runs_from_sessions({"switch.a": _session(1, 30, sid=None)}, NOW)
    resolved = resolve_conflicts(committed, POLICY_DEFER_NEW, compress_floor_pct=100)
    due = due_runs_since(resolved, NOW - timedelta(minutes=5), NOW)
    assert any(r.reason == REASON_COMMITTED for r in due)  # it IS in the window
    fireable = [r for r in due if r.reason != REASON_COMMITTED]  # coordinator's guard
    assert fireable == []
