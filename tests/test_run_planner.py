"""Tests for RunPlanner.next_runs (pure logic).

Given a list of Schedule rules + a time window, return all scheduled
runs in that window, sorted chronologically. No HA, no clock — the
caller supplies `from_dt` and `until_dt`.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from custom_components.complete_irrigation.run_planner import (
    PlannedRun,
    next_runs,
)
from custom_components.complete_irrigation.schedule import Schedule

# 2026-05-18 is a Monday. weekday() returns:
#   Mon=0 Tue=1 Wed=2 Thu=3 Fri=4 Sat=5 Sun=6
MONDAY_6AM = datetime(2026, 5, 18, 6, 0, 0)
MONDAY_MIDNIGHT = datetime(2026, 5, 18, 0, 0, 0)


def _sched(**overrides) -> Schedule:
    defaults = {
        "id": "s1",
        "name": "Morning Front",
        "zone_entity_id": "switch.front_lawn",
        "start_time": time(6, 0),
        "duration_minutes": 15,
        "weekdays": (0, 1, 2, 3, 4, 5, 6),  # daily
    }
    defaults.update(overrides)
    return Schedule(**defaults)


# ── happy paths ───────────────────────────────────────────────────


def test_empty_schedules_returns_empty():
    runs = next_runs([], from_dt=MONDAY_MIDNIGHT, until_dt=MONDAY_MIDNIGHT + timedelta(days=14))
    assert runs == []


def test_single_daily_schedule_over_14d_window_yields_14_runs():
    runs = next_runs(
        [_sched()],
        from_dt=MONDAY_MIDNIGHT,
        until_dt=MONDAY_MIDNIGHT + timedelta(days=14),
    )
    assert len(runs) == 14
    # First is today at 6 AM
    assert runs[0].start_at == MONDAY_6AM
    # Last is 13 days later at 6 AM
    assert runs[-1].start_at == MONDAY_6AM + timedelta(days=13)


def test_weekdays_only_schedule_skips_weekend():
    """Mon-Fri schedule should produce 5 runs in a 7-day window starting Mon."""
    runs = next_runs(
        [_sched(weekdays=(0, 1, 2, 3, 4))],
        from_dt=MONDAY_MIDNIGHT,
        until_dt=MONDAY_MIDNIGHT + timedelta(days=7),
    )
    assert len(runs) == 5
    # Make sure they're all weekdays
    for r in runs:
        assert r.start_at.weekday() in {0, 1, 2, 3, 4}


def test_window_starting_after_today_run_skips_today():
    """If 'now' is 7 AM and the daily 6 AM run already happened, today's
    run shouldn't appear in the plan."""
    runs = next_runs(
        [_sched()],
        from_dt=MONDAY_6AM + timedelta(hours=1),  # 7 AM
        until_dt=MONDAY_MIDNIGHT + timedelta(days=2),  # through next 2 days
    )
    # Today (Mon) at 6 AM was before our from_dt, so should be skipped.
    # Tomorrow (Tue) at 6 AM should be the first.
    assert len(runs) == 1
    assert runs[0].start_at == MONDAY_6AM + timedelta(days=1)


def test_window_ending_before_next_match_returns_empty():
    """Saturday-only schedule, window is Mon-Fri → no runs."""
    runs = next_runs(
        [_sched(weekdays=(5,))],  # Sat
        from_dt=MONDAY_MIDNIGHT,
        until_dt=MONDAY_MIDNIGHT + timedelta(days=4),  # Mon-Thu
    )
    assert runs == []


# ── disabled schedules ────────────────────────────────────────────


def test_disabled_schedule_produces_no_runs():
    runs = next_runs(
        [_sched(enabled=False)],
        from_dt=MONDAY_MIDNIGHT,
        until_dt=MONDAY_MIDNIGHT + timedelta(days=14),
    )
    assert runs == []


def test_disabled_does_not_affect_other_enabled_schedules():
    runs = next_runs(
        [
            _sched(id="off", name="Off", enabled=False),
            _sched(id="on", name="On"),
        ],
        from_dt=MONDAY_MIDNIGHT,
        until_dt=MONDAY_MIDNIGHT + timedelta(days=3),
    )
    assert len(runs) == 3
    assert all(r.schedule_id == "on" for r in runs)


# ── sorted output ─────────────────────────────────────────────────


def test_results_sorted_chronologically_across_schedules():
    morning = _sched(id="m", name="Morning", start_time=time(6, 0))
    evening = _sched(id="e", name="Evening", start_time=time(18, 0))
    runs = next_runs(
        [evening, morning],  # passed in reverse order
        from_dt=MONDAY_MIDNIGHT,
        until_dt=MONDAY_MIDNIGHT + timedelta(days=2),
    )
    # Expected: Mon morning, Mon evening, Tue morning, Tue evening
    assert [r.schedule_id for r in runs] == ["m", "e", "m", "e"]
    starts = [r.start_at for r in runs]
    assert starts == sorted(starts)


# ── PlannedRun fields ─────────────────────────────────────────────


def test_planned_run_carries_zone_duration_schedule_id_and_name():
    runs = next_runs(
        [_sched(zone_entity_id="switch.back_lawn", duration_minutes=22)],
        from_dt=MONDAY_MIDNIGHT,
        until_dt=MONDAY_MIDNIGHT + timedelta(days=1),
    )
    assert len(runs) == 1
    r = runs[0]
    assert isinstance(r, PlannedRun)
    assert r.zone_entity_id == "switch.back_lawn"
    assert r.duration_minutes == 22
    assert r.schedule_id == "s1"
    assert r.schedule_name == "Morning Front"
    assert r.reason == "scheduled"


# ── edge: empty window ────────────────────────────────────────────


def test_zero_length_window_returns_empty():
    runs = next_runs(
        [_sched()],
        from_dt=MONDAY_6AM,
        until_dt=MONDAY_6AM,
    )
    assert runs == []


def test_negative_window_returns_empty():
    runs = next_runs(
        [_sched()],
        from_dt=MONDAY_6AM + timedelta(days=1),
        until_dt=MONDAY_6AM,
    )
    assert runs == []
