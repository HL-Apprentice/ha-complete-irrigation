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
) -> list[PlannedRun]:
    """Return all runs in [from_dt, until_dt) sorted by start_at."""
    if until_dt <= from_dt:
        return []

    runs: list[PlannedRun] = []
    for sched in schedules:
        if not sched.enabled:
            continue
        runs.extend(_runs_for_schedule(sched, from_dt, until_dt))

    return sorted(runs, key=lambda r: r.start_at)


def _runs_for_schedule(
    sched: Schedule,
    from_dt: datetime,
    until_dt: datetime,
) -> Iterable[PlannedRun]:
    """All firings of one Schedule in the window.

    Branches on `sched.mode`:
      • "weekdays" — fires on listed weekdays every week
      • "interval" — fires every `interval_days` days from `interval_anchor`

    Preserves `from_dt`'s tzinfo so aware/naive comparisons don't blow up.
    """
    from .schedule import MODE_INTERVAL

    if sched.mode == MODE_INTERVAL:
        yield from _runs_for_interval(sched, from_dt, until_dt)
    else:
        yield from _runs_for_weekdays(sched, from_dt, until_dt)


def _expand_steps(sched: Schedule, base_start: datetime) -> Iterable[PlannedRun]:
    """For a single firing at `base_start`, emit one PlannedRun per zone-step.

    Single-zone schedules (the common case) emit exactly one PlannedRun.
    Multi-zone schedules emit one per step, each starting after the
    previous one's duration + DEFAULT_ZONE_BUFFER_SECONDS (valve buffer).
    """
    from .schedule import DEFAULT_ZONE_BUFFER_SECONDS

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
            seconds=DEFAULT_ZONE_BUFFER_SECONDS,
        )


def _runs_for_weekdays(sched, from_dt, until_dt):
    tz = from_dt.tzinfo
    weekday_set = set(sched.weekdays)
    current = from_dt.date()
    end = until_dt.date()
    if sched.end_date is not None and sched.end_date < end:
        end = sched.end_date

    while current <= end:
        if current.weekday() in weekday_set:
            base_start = datetime.combine(current, sched.start_time, tzinfo=tz)
            for run in _expand_steps(sched, base_start):
                # Window check on each step individually so a multi-zone
                # firing that begins inside the window but spills past
                # until_dt still includes its early steps.
                if from_dt <= run.start_at < until_dt:
                    yield run
        current += timedelta(days=1)


def _runs_for_interval(sched, from_dt, until_dt):
    """Generate dates every `interval_days` days starting at `interval_anchor`.
    Skip past dates before the window, stop at end_date / window end."""
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
    if sched.end_date is not None and sched.end_date < end:
        end = sched.end_date

    while current <= end:
        base_start = datetime.combine(current, sched.start_time, tzinfo=tz)
        for run in _expand_steps(sched, base_start):
            if from_dt <= run.start_at < until_dt:
                yield run
        current += timedelta(days=step)


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
