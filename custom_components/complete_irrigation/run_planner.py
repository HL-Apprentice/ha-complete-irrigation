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
    """All firings of one Schedule in the window."""
    weekday_set = set(sched.weekdays)
    current = from_dt.date()
    end = until_dt.date()

    while current <= end:
        if current.weekday() in weekday_set:
            run_at = datetime.combine(current, sched.start_time)
            if from_dt <= run_at < until_dt:
                yield PlannedRun(
                    zone_entity_id=sched.zone_entity_id,
                    start_at=run_at,
                    duration_minutes=sched.duration_minutes,
                    schedule_id=sched.id,
                    schedule_name=sched.name,
                )
        current += timedelta(days=1)
