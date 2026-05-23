"""Tests for RunPlanner.next_runs (pure logic).

Given a list of Schedule rules + a time window, return all scheduled
runs in that window, sorted chronologically. No HA, no clock — the
caller supplies `from_dt` and `until_dt`.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from custom_components.complete_irrigation.run_planner import (
    PlannedRun,
    due_runs_since,
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


# ── tzinfo preservation (HA passes aware datetimes) ───────────────


def test_aware_datetimes_preserved_in_planned_runs():
    """When the caller passes tz-aware datetimes, generated PlannedRun
    start_at values must also be aware in the same tz — otherwise
    HA's comparisons throw 'naive vs aware' errors."""
    aware_start = MONDAY_MIDNIGHT.replace(tzinfo=timezone.utc)  # noqa: UP017
    aware_end = aware_start + timedelta(days=2)
    runs = next_runs([_sched()], from_dt=aware_start, until_dt=aware_end)
    assert len(runs) == 2
    for r in runs:
        assert r.start_at.tzinfo is not None


# ════════════════════════════════════════════════════════════════════
# due_runs_since — coordinator's per-tick filter
# ════════════════════════════════════════════════════════════════════


def _planned(start_at: datetime, sid: str = "x") -> PlannedRun:
    return PlannedRun(
        zone_entity_id="switch.x",
        start_at=start_at,
        duration_minutes=10,
        schedule_id=sid,
        schedule_name="X",
    )


def test_due_runs_includes_runs_in_half_open_interval():
    """(last_tick, now] — exclusive of last_tick, inclusive of now."""
    last = MONDAY_6AM
    now = MONDAY_6AM + timedelta(seconds=60)
    runs = [
        _planned(MONDAY_6AM, "at_last"),  # exactly at last → excluded
        _planned(MONDAY_6AM + timedelta(seconds=30), "between"),  # included
        _planned(now, "at_now"),  # included
        _planned(now + timedelta(seconds=1), "after"),  # excluded
    ]
    due = due_runs_since(runs, last, now)
    assert [r.schedule_id for r in due] == ["between", "at_now"]


def test_due_runs_empty_when_nothing_in_window():
    runs = [_planned(MONDAY_6AM + timedelta(hours=2))]
    due = due_runs_since(runs, MONDAY_6AM, MONDAY_6AM + timedelta(minutes=1))
    assert due == []


def test_due_runs_empty_input_returns_empty():
    assert due_runs_since([], MONDAY_6AM, MONDAY_6AM + timedelta(minutes=1)) == []


# ════════════════════════════════════════════════════════════════════
# Interval-mode schedules (v1.4)
# ════════════════════════════════════════════════════════════════════


def _interval_sched(**kw):
    from datetime import date

    defaults = {
        "id": "tree",
        "name": "Tree deep watering",
        "zone_entity_id": "switch.trees",
        "start_time": time(2, 0),
        "duration_minutes": 300,
        "weekdays": (),
        "mode": "interval",
        "interval_days": 5,
        "interval_anchor": date(2026, 5, 18),  # Monday
    }
    defaults.update(kw)
    return Schedule(**defaults)


def test_interval_runs_every_n_days_from_anchor():
    """Anchor = Mon May 18, every 5 days → fires May 18, May 23, May 28."""
    runs = next_runs(
        [_interval_sched()],
        from_dt=MONDAY_MIDNIGHT,
        until_dt=MONDAY_MIDNIGHT + timedelta(days=14),
    )
    starts = [(r.start_at.month, r.start_at.day, r.start_at.hour) for r in runs]
    assert starts == [(5, 18, 2), (5, 23, 2), (5, 28, 2)]


def test_interval_skips_past_dates_before_window():
    """Anchor in the past — only future runs in window appear."""
    from datetime import date

    runs = next_runs(
        [_interval_sched(interval_anchor=date(2026, 5, 1), interval_days=3)],
        from_dt=MONDAY_MIDNIGHT,  # 2026-05-18
        until_dt=MONDAY_MIDNIGHT + timedelta(days=10),
    )
    # May 1 + N*3 in window [May 18, May 28): May 19 (1+6*3), May 22, May 25
    starts = [(r.start_at.month, r.start_at.day) for r in runs]
    assert (5, 19) in starts
    assert (5, 22) in starts
    assert (5, 25) in starts


def test_interval_respects_end_date():
    from datetime import date

    runs = next_runs(
        [_interval_sched(end_date=date(2026, 5, 22))],
        from_dt=MONDAY_MIDNIGHT,
        until_dt=MONDAY_MIDNIGHT + timedelta(days=14),
    )
    starts = [(r.start_at.month, r.start_at.day) for r in runs]
    assert starts == [(5, 18)]  # Next firing (5, 23) is past end_date


def test_interval_skipped_when_disabled():
    runs = next_runs(
        [_interval_sched(enabled=False)],
        from_dt=MONDAY_MIDNIGHT,
        until_dt=MONDAY_MIDNIGHT + timedelta(days=14),
    )
    assert runs == []


# ════════════════════════════════════════════════════════════════════
# Multi-zone schedules (v1.8) — expanded into back-to-back PlannedRuns
# ════════════════════════════════════════════════════════════════════


def test_multi_zone_schedule_expands_to_one_run_per_step():
    """A 3-step schedule should yield 3 PlannedRuns per firing day."""
    from custom_components.complete_irrigation.schedule import ZoneStep

    sched = _sched(
        weekdays=(0,),  # Monday only
        zone_steps=(
            ZoneStep("switch.front_lawn", 15),
            ZoneStep("switch.back_lawn", 10),
            ZoneStep("switch.garden", 5),
        ),
    )
    runs = next_runs([sched], from_dt=MONDAY_MIDNIGHT, until_dt=MONDAY_MIDNIGHT + timedelta(days=2))
    # One Monday firing x 3 steps = 3 PlannedRuns
    assert len(runs) == 3
    assert runs[0].zone_entity_id == "switch.front_lawn"
    assert runs[1].zone_entity_id == "switch.back_lawn"
    assert runs[2].zone_entity_id == "switch.garden"


def test_multi_zone_steps_run_back_to_back_with_30s_buffer():
    """Step N+1 starts at step N's end + 30 seconds (default buffer)."""
    from custom_components.complete_irrigation.schedule import ZoneStep

    sched = _sched(
        weekdays=(0,),
        zone_steps=(
            ZoneStep("switch.front_lawn", 15),
            ZoneStep("switch.back_lawn", 10),
        ),
    )
    runs = next_runs([sched], from_dt=MONDAY_MIDNIGHT, until_dt=MONDAY_MIDNIGHT + timedelta(days=1))
    assert len(runs) == 2
    # Step 0: starts at 06:00, runs 15 min → ends 06:15
    assert runs[0].start_at == MONDAY_6AM
    # Step 1: starts at 06:15 + 30s
    expected = MONDAY_6AM + timedelta(minutes=15, seconds=30)
    assert runs[1].start_at == expected


def test_multi_zone_each_step_carries_own_duration():
    """Each PlannedRun should reflect its own step's duration, not the schedule's top-level."""
    from custom_components.complete_irrigation.schedule import ZoneStep

    sched = _sched(
        duration_minutes=15,
        weekdays=(0,),
        zone_steps=(
            ZoneStep("switch.front_lawn", 15),
            ZoneStep("switch.back_lawn", 8),
            ZoneStep("switch.garden", 22),
        ),
    )
    runs = next_runs([sched], from_dt=MONDAY_MIDNIGHT, until_dt=MONDAY_MIDNIGHT + timedelta(days=1))
    assert [r.duration_minutes for r in runs] == [15, 8, 22]


def test_single_zone_schedule_still_yields_one_run():
    """Backward compat: zone_steps empty → exactly one PlannedRun per firing."""
    runs = next_runs(
        [_sched(weekdays=(0,))],
        from_dt=MONDAY_MIDNIGHT,
        until_dt=MONDAY_MIDNIGHT + timedelta(days=1),
    )
    assert len(runs) == 1
    assert runs[0].zone_entity_id == "switch.front_lawn"


def test_multi_zone_with_interval_mode_works():
    """Interval-mode schedules also expand step-by-step."""
    from datetime import date

    from custom_components.complete_irrigation.schedule import ZoneStep

    sched = Schedule(
        id="multi",
        name="Multi Zone Interval",
        zone_entity_id="switch.front_lawn",
        start_time=time(6, 0),
        duration_minutes=10,
        weekdays=(),
        mode="interval",
        interval_days=2,
        interval_anchor=date(2026, 5, 18),
        zone_steps=(
            ZoneStep("switch.front_lawn", 10),
            ZoneStep("switch.back_lawn", 5),
        ),
    )
    runs = next_runs([sched], from_dt=MONDAY_MIDNIGHT, until_dt=MONDAY_MIDNIGHT + timedelta(days=4))
    # 2 firings (day 0 and day 2) x 2 steps = 4 runs
    assert len(runs) == 4


# ════════════════════════════════════════════════════════════════════
# Interval-hours mode (v1.11) — fires every N hours from anchor + start_time
# ════════════════════════════════════════════════════════════════════


def _hourly_sched(**kw):
    from datetime import date

    defaults = {
        "id": "h1",
        "name": "Hourly veg",
        "zone_entity_id": "switch.veg",
        "start_time": time(6, 0),
        "duration_minutes": 5,
        "weekdays": (),
        "mode": "interval_hours",
        "interval_hours": 6,
        "interval_anchor": date(2026, 5, 18),  # Monday
    }
    defaults.update(kw)
    return Schedule(**defaults)


def test_interval_hours_4_runs_in_24h_from_anchor():
    """6-hour cycles starting Mon 06:00 should fire at 06, 12, 18, 00 (Tue)."""
    runs = next_runs(
        [_hourly_sched()],
        from_dt=MONDAY_MIDNIGHT,
        until_dt=MONDAY_MIDNIGHT + timedelta(days=1),
    )
    times = [(r.start_at.hour, r.start_at.minute) for r in runs]
    # Anchor is 06:00; from_dt is 00:00 same day, so we miss earlier than 06.
    # 6h cycles: 06, 12, 18 (all on Monday). Next is 00:00 Tuesday → outside window.
    assert times == [(6, 0), (12, 0), (18, 0)]


def test_interval_hours_crosses_day_boundary():
    """8-hour cycles starting Mon 06:00 → next runs at 14, 22, then 06 Tue."""
    runs = next_runs(
        [_hourly_sched(interval_hours=8)],
        from_dt=MONDAY_MIDNIGHT,
        until_dt=MONDAY_MIDNIGHT + timedelta(days=2),
    )
    times = [(r.start_at.day, r.start_at.hour) for r in runs]
    # Mon 06, Mon 14, Mon 22, Tue 06, Tue 14, Tue 22
    assert times == [(18, 6), (18, 14), (18, 22), (19, 6), (19, 14), (19, 22)]


def test_interval_hours_skips_runs_before_anchor():
    """A 6h schedule whose anchor is Tuesday shouldn't fire on Monday."""
    from datetime import date

    runs = next_runs(
        [_hourly_sched(interval_anchor=date(2026, 5, 19))],  # Tuesday
        from_dt=MONDAY_MIDNIGHT,
        until_dt=MONDAY_MIDNIGHT + timedelta(days=2),
    )
    # Should only see Tue runs: 06, 12, 18 (next is 00 Wed, outside window)
    days = {r.start_at.day for r in runs}
    assert days == {19}


def test_interval_hours_respects_end_date():
    from datetime import date

    runs = next_runs(
        [_hourly_sched(interval_hours=12, end_date=date(2026, 5, 18))],
        from_dt=MONDAY_MIDNIGHT,
        until_dt=MONDAY_MIDNIGHT + timedelta(days=5),
    )
    # 12h cycles from Mon 06:00, capped at end_date Mon:
    # Mon 06, Mon 18 — and we stop before Tue 06.
    days = {(r.start_at.day, r.start_at.hour) for r in runs}
    assert days == {(18, 6), (18, 18)}


def test_interval_hours_skipped_when_disabled():
    runs = next_runs(
        [_hourly_sched(enabled=False)],
        from_dt=MONDAY_MIDNIGHT,
        until_dt=MONDAY_MIDNIGHT + timedelta(days=1),
    )
    assert runs == []


def test_interval_hours_from_dt_after_anchor_advances_correctly():
    """When the planner window starts after the anchor, we should fast-
    forward to the next cycle inside the window."""
    runs = next_runs(
        [_hourly_sched(interval_hours=6)],
        from_dt=MONDAY_6AM + timedelta(hours=7),  # Mon 13:00
        until_dt=MONDAY_MIDNIGHT + timedelta(days=1),
    )
    # First cycle ≥ 13:00 with anchor 06:00 + 6h steps is 18:00.
    times = [(r.start_at.hour, r.start_at.minute) for r in runs]
    assert times == [(18, 0)]


# ════════════════════════════════════════════════════════════════════
# Active period filtering in the planner (v1.12)
# ════════════════════════════════════════════════════════════════════


def test_planner_respects_start_date_one_shot():
    """Schedule with start_date in the future shouldn't fire today."""
    from datetime import date

    s = _sched(start_date=date(2026, 5, 25))  # week after MONDAY_MIDNIGHT
    runs = next_runs(
        [s],
        from_dt=MONDAY_MIDNIGHT,
        until_dt=MONDAY_MIDNIGHT + timedelta(days=3),
    )
    # All runs in this window are before May 25, should be empty
    assert runs == []


def test_planner_respects_end_date_one_shot():
    """Schedule with end_date in the past shouldn't fire."""
    from datetime import date

    s = _sched(end_date=date(2026, 5, 15))  # before MONDAY_MIDNIGHT (May 18)
    runs = next_runs(
        [s],
        from_dt=MONDAY_MIDNIGHT,
        until_dt=MONDAY_MIDNIGHT + timedelta(days=14),
    )
    assert runs == []


def test_planner_fires_within_one_shot_window():
    """Schedule with both bounds fires inside the window."""
    from datetime import date

    s = _sched(
        start_date=date(2026, 5, 18),  # MONDAY
        end_date=date(2026, 5, 22),  # Friday
    )
    runs = next_runs(
        [s],
        from_dt=MONDAY_MIDNIGHT,
        until_dt=MONDAY_MIDNIGHT + timedelta(days=10),
    )
    # 5 daily runs Mon-Fri only (Sat May 23 + later are after end_date)
    assert len(runs) == 5
    days = [r.start_at.day for r in runs]
    assert days == [18, 19, 20, 21, 22]


def test_planner_annual_repeat_fires_each_year():
    """A March-October window with repeat_annually fires both years
    when the planner window straddles two springs."""
    from datetime import date

    s = _sched(
        weekdays=(0,),  # Monday only
        start_date=date(2026, 5, 1),
        end_date=date(2026, 6, 30),
        repeat_annually=True,
    )
    # Window from Mon May 18 2026 → +400 days lands in mid 2027
    runs = next_runs(
        [s],
        from_dt=MONDAY_MIDNIGHT,
        until_dt=MONDAY_MIDNIGHT + timedelta(days=400),
    )
    # 2026 Mondays in May/June: 18, 25 (May); 1, 8, 15, 22, 29 (June) = 7
    # 2027 Mondays in May/June (5/3, 5/10, ..., 6/28) = 9
    # Out of window months should be excluded.
    months = {(r.start_at.year, r.start_at.month) for r in runs}
    assert months == {(2026, 5), (2026, 6), (2027, 5), (2027, 6)}


def test_planner_interval_mode_honors_active_window():
    """interval-days fires only inside the window."""
    from datetime import date

    s = Schedule(
        id="i",
        name="i",
        zone_entity_id="switch.x",
        start_time=time(6, 0),
        duration_minutes=10,
        weekdays=(),
        mode="interval",
        interval_days=3,
        interval_anchor=date(2026, 5, 18),
        start_date=date(2026, 5, 20),
        end_date=date(2026, 5, 26),
    )
    runs = next_runs(
        [s],
        from_dt=MONDAY_MIDNIGHT,
        until_dt=MONDAY_MIDNIGHT + timedelta(days=14),
    )
    # Cycle anchor May 18 + 3-day step: 18, 21, 24, 27, 30...
    # But window is May 20-26, so only 21 and 24 should fire.
    days = sorted(r.start_at.day for r in runs)
    assert days == [21, 24]
