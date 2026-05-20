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

    Preserves `from_dt`'s tzinfo so naive ↔ aware comparisons don't blow up.
    HA passes tz-aware datetimes; tests use naive.
    """
    tz = from_dt.tzinfo
    weekday_set = set(sched.weekdays)
    current = from_dt.date()
    end = until_dt.date()
    if sched.end_date is not None and sched.end_date < end:
        end = sched.end_date

    while current <= end:
        if current.weekday() in weekday_set:
            run_at = datetime.combine(current, sched.start_time, tzinfo=tz)
            if from_dt <= run_at < until_dt:
                yield PlannedRun(
                    zone_entity_id=sched.zone_entity_id,
                    start_at=run_at,
                    duration_minutes=sched.duration_minutes,
                    schedule_id=sched.id,
                    schedule_name=sched.name,
                )
        current += timedelta(days=1)


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
