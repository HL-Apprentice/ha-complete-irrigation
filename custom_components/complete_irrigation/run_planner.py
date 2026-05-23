"""RunPlanner — pure-logic projection of Schedules onto a time window.

`next_runs(schedules, from_dt, until_dt)` returns every enabled schedule's
firings in the half-open interval [from_dt, until_dt) sorted by start
time. No clock, no HA — the caller supplies the bounds.

The HA-coupled coordinator wraps this in a tick loop: each tick computes
the next ~15 minutes of runs and fires any whose start time has just
been reached.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from .schedule import Schedule


@dataclass(frozen=True)
class PlannedRun:
    """A single firing of a Schedule, materialized for a specific moment."""

    zone_entity_id: str
    start_at: datetime
    duration_minutes: int
    schedule_id: str
    schedule_name: str
    reason: str = "scheduled"


def next_runs(
    schedules: Iterable[Schedule],
    from_dt: datetime,
    until_dt: datetime,
    *,
    zone_buffer_seconds: int | None = None,
) -> list[PlannedRun]:
    """Return all runs in [from_dt, until_dt) sorted by start_at.

    `zone_buffer_seconds` controls the inter-zone valve-settle gap for
    multi-zone schedules (PRD #38). Defaults to schedule.DEFAULT_ZONE_BUFFER_SECONDS
    when None.
    """
    if until_dt <= from_dt:
        return []

    runs: list[PlannedRun] = []
    for sched in schedules:
        if not sched.enabled:
            continue
        runs.extend(_runs_for_schedule(sched, from_dt, until_dt, zone_buffer_seconds))

    return sorted(runs, key=lambda r: r.start_at)


def _runs_for_schedule(
    sched: Schedule,
    from_dt: datetime,
    until_dt: datetime,
    zone_buffer_seconds: int | None = None,
) -> Iterable[PlannedRun]:
    """All firings of one Schedule in the window.

    Branches on `sched.mode`:
      • "weekdays"       — fires on listed weekdays every week
      • "interval"       — every `interval_days` days from `interval_anchor`
      • "interval_hours" — every `interval_hours` hours from
        interval_anchor + start_time, crossing day boundaries

    Preserves `from_dt`'s tzinfo so aware/naive comparisons don't blow up.
    """
    from .schedule import MODE_INTERVAL, MODE_INTERVAL_HOURS

    if sched.mode == MODE_INTERVAL:
        yield from _runs_for_interval(sched, from_dt, until_dt, zone_buffer_seconds)
    elif sched.mode == MODE_INTERVAL_HOURS:
        yield from _runs_for_interval_hours(sched, from_dt, until_dt, zone_buffer_seconds)
    else:
        yield from _runs_for_weekdays(sched, from_dt, until_dt, zone_buffer_seconds)


def _expand_steps(
    sched: Schedule, base_start: datetime, buffer_seconds: int | None = None
) -> Iterable[PlannedRun]:
    """For a single firing at `base_start`, emit one PlannedRun per zone-step.

    Single-zone schedules (the common case) emit exactly one PlannedRun.
    Multi-zone schedules emit one per step, each starting after the
    previous one's duration + buffer (defaults to DEFAULT_ZONE_BUFFER_SECONDS).
    """
    from .schedule import DEFAULT_ZONE_BUFFER_SECONDS

    buf = DEFAULT_ZONE_BUFFER_SECONDS if buffer_seconds is None else int(buffer_seconds)
    cursor = base_start
    for step in sched.all_steps():
        yield PlannedRun(
            zone_entity_id=step.zone_entity_id,
            start_at=cursor,
            duration_minutes=step.duration_minutes,
            schedule_id=sched.id,
            schedule_name=sched.name,
        )
        cursor = cursor + timedelta(
            minutes=step.duration_minutes,
            seconds=buf,
        )


def _runs_for_weekdays(sched, from_dt, until_dt, buffer_seconds=None):
    from .schedule import is_within_active_window

    tz = from_dt.tzinfo
    weekday_set = set(sched.weekdays)
    current = from_dt.date()
    end = until_dt.date()
    # For one-shot non-repeating schedules, the end_date narrows the
    # planning window. For repeat_annually schedules the per-year active
    # window may be larger or smaller than end_date — handled per-day
    # via is_within_active_window below.
    if sched.end_date is not None and not sched.repeat_annually and sched.end_date < end:
        end = sched.end_date

    while current <= end:
        if current.weekday() in weekday_set and is_within_active_window(sched, current):
            base_start = datetime.combine(current, sched.start_time, tzinfo=tz)
            for run in _expand_steps(sched, base_start, buffer_seconds):
                # Window check on each step individually so a multi-zone
                # firing that begins inside the window but spills past
                # until_dt still includes its early steps.
                if from_dt <= run.start_at < until_dt:
                    yield run
        current += timedelta(days=1)


def _runs_for_interval(sched, from_dt, until_dt, buffer_seconds=None):
    """Generate dates every `interval_days` days starting at `interval_anchor`.
    Skip past dates before the window, stop at end_date / window end."""
    from .schedule import is_within_active_window

    tz = from_dt.tzinfo
    step = sched.interval_days
    anchor = sched.interval_anchor

    # Advance the anchor to the first date >= from_dt.date()
    current = anchor
    if current < from_dt.date():
        # Number of steps to skip to reach the window
        delta_days = (from_dt.date() - anchor).days
        skip_steps = (delta_days + step - 1) // step  # ceil-divide
        current = anchor + timedelta(days=skip_steps * step)

    end = until_dt.date()
    if sched.end_date is not None and not sched.repeat_annually and sched.end_date < end:
        end = sched.end_date

    while current <= end:
        if is_within_active_window(sched, current):
            base_start = datetime.combine(current, sched.start_time, tzinfo=tz)
            for run in _expand_steps(sched, base_start, buffer_seconds):
                if from_dt <= run.start_at < until_dt:
                    yield run
        current += timedelta(days=step)


def _runs_for_interval_hours(sched, from_dt, until_dt, buffer_seconds=None):
    """Generate runs every `interval_hours` from interval_anchor + start_time.

    Unlike interval-days mode, this crosses day boundaries: an "every 6h
    starting at 06:00" schedule fires at 06:00, 12:00, 18:00, 00:00,
    06:00 the next day, ... continuing until end_date or window end.
    Skips runs before interval_anchor; stops at end_date if set.
    """
    from .schedule import is_within_active_window

    tz = from_dt.tzinfo
    step_hours = sched.interval_hours
    anchor_dt = datetime.combine(sched.interval_anchor, sched.start_time, tzinfo=tz)
    step = timedelta(hours=step_hours)

    # Advance to the first firing >= from_dt
    current = anchor_dt
    if current < from_dt:
        # Calculate how many steps to skip to land at or after from_dt
        elapsed = from_dt - anchor_dt
        n_steps = elapsed // step  # integer floor division on timedelta
        current = anchor_dt + n_steps * step
        # If still before from_dt, advance one more step
        if current < from_dt:
            current += step

    end_dt = until_dt
    # Only narrow window for non-repeating end_date; annual repeat is
    # checked per-cycle via is_within_active_window below.
    if sched.end_date is not None and not sched.repeat_annually:
        # Cap at end-of-day on end_date so runs before midnight are kept.
        end_of_end = datetime.combine(sched.end_date, sched.start_time, tzinfo=tz) + timedelta(
            days=1
        )
        if end_of_end < end_dt:
            end_dt = end_of_end

    while current < end_dt:
        if is_within_active_window(sched, current.date()):
            for run in _expand_steps(sched, current, buffer_seconds):
                if from_dt <= run.start_at < until_dt:
                    yield run
        current += step


def due_runs_since(
    runs: Iterable[PlannedRun],
    last_tick: datetime,
    now: datetime,
) -> list[PlannedRun]:
    """Runs whose start_at falls in (last_tick, now].

    Used by the coordinator each tick to find what to fire. The
    half-open interval prevents double-firing across consecutive ticks.
    """
    return [r for r in runs if last_tick < r.start_at <= now]
