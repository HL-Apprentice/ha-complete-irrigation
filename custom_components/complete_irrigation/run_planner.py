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

from .schedule import (
    ANCHOR_FINISH,
    DEFAULT_ZONE_BUFFER_SECONDS,
    MODE_INTERVAL,
    MODE_INTERVAL_HOURS,
    Schedule,
    is_within_active_window,
)


@dataclass(frozen=True)
class PlannedRun:
    """A single firing of a Schedule, materialized for a specific moment."""

    zone_entity_id: str
    start_at: datetime
    duration_minutes: int
    schedule_id: str
    schedule_name: str
    reason: str = "scheduled"
    # v1.20 — the original scheduled start, preserved through conflict
    # resolution (which rewrites start_at). The coordinator keys its
    # fired-runs ledger on this so a deferred run is never re-planned as a
    # fresh firing and lost. Defaults to start_at when not set explicitly.
    scheduled_start_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.scheduled_start_at is None:
            object.__setattr__(self, "scheduled_start_at", self.start_at)


def next_runs(
    schedules: Iterable[Schedule],
    from_dt: datetime,
    until_dt: datetime,
    *,
    zone_buffer_seconds: int | None = None,
    sun_times=None,
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
        runs.extend(_runs_for_schedule(sched, from_dt, until_dt, zone_buffer_seconds, sun_times))

    return sorted(runs, key=lambda r: r.start_at)


def _runs_for_schedule(
    sched: Schedule,
    from_dt: datetime,
    until_dt: datetime,
    zone_buffer_seconds: int | None = None,
    sun_times=None,
) -> Iterable[PlannedRun]:
    """All firings of one Schedule in the window.

    Branches on `sched.mode`:
      • "weekdays"       — fires on listed weekdays every week
      • "interval"       — every `interval_days` days from `interval_anchor`
      • "interval_hours" — every `interval_hours` hours from
        interval_anchor + start_time, crossing day boundaries

    Preserves `from_dt`'s tzinfo so aware/naive comparisons don't blow up.
    """
    if sched.mode == MODE_INTERVAL:
        yield from _runs_for_interval(sched, from_dt, until_dt, zone_buffer_seconds, sun_times)
    elif sched.mode == MODE_INTERVAL_HOURS:
        yield from _runs_for_interval_hours(sched, from_dt, until_dt, zone_buffer_seconds)
    else:
        yield from _runs_for_weekdays(sched, from_dt, until_dt, zone_buffer_seconds, sun_times)


def _expand_steps(
    sched: Schedule, base_start: datetime, buffer_seconds: int | None = None
) -> Iterable[PlannedRun]:
    """For a single firing at `base_start`, emit one PlannedRun per zone-step.

    Single-zone schedules (the common case) emit exactly one PlannedRun.
    Multi-zone schedules emit one per step, each starting after the
    previous one's duration + buffer (defaults to DEFAULT_ZONE_BUFFER_SECONDS).
    """
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


def _base_start_for(sched, day, tz, buffer_seconds, sun_times):
    """The concrete start datetime for one occurrence on `day`.

    v1.40 — when the schedule is sun-anchored and the provider yields times
    for the day, resolve sunrise/sunset ± offset; anchor="finish" backs the
    start up by the schedule's total span so the firing COMPLETES at the
    resolved moment. Falls back to the fixed start_time whenever sun times
    are unavailable (provider absent/failed) — a sun schedule never goes
    silent, it just runs at its fallback time.
    """
    if sched.sun_event is not None and sun_times is not None:
        st = None
        try:
            st = sun_times(day)
        except Exception:  # provider failure -> fixed-time fallback
            st = None
        if st is not None:
            ev = st[0] if sched.sun_event == "sunrise" else st[1]
            dt = ev + timedelta(minutes=sched.sun_offset_minutes)
            if sched.anchor == ANCHOR_FINISH:
                dt -= timedelta(minutes=sched.total_span_minutes(buffer_seconds))
            if tz is not None and dt.tzinfo is not None:
                dt = dt.astimezone(tz)
            return dt
    return datetime.combine(day, sched.start_time, tzinfo=tz)


def _runs_for_weekdays(
    sched: Schedule,
    from_dt: datetime,
    until_dt: datetime,
    buffer_seconds: int | None = None,
    sun_times=None,
) -> Iterable[PlannedRun]:
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
            base_start = _base_start_for(sched, current, tz, buffer_seconds, sun_times)
            for run in _expand_steps(sched, base_start, buffer_seconds):
                # Window check on each step individually so a multi-zone
                # firing that begins inside the window but spills past
                # until_dt still includes its early steps.
                if from_dt <= run.start_at < until_dt:
                    yield run
        current += timedelta(days=1)


def _runs_for_interval(
    sched: Schedule,
    from_dt: datetime,
    until_dt: datetime,
    buffer_seconds: int | None = None,
    sun_times=None,
) -> Iterable[PlannedRun]:
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
    if sched.end_date is not None and not sched.repeat_annually and sched.end_date < end:
        end = sched.end_date

    while current <= end:
        if is_within_active_window(sched, current):
            base_start = _base_start_for(sched, current, tz, buffer_seconds, sun_times)
            for run in _expand_steps(sched, base_start, buffer_seconds):
                if from_dt <= run.start_at < until_dt:
                    yield run
        current += timedelta(days=step)


def _runs_for_interval_hours(
    sched: Schedule,
    from_dt: datetime,
    until_dt: datetime,
    buffer_seconds: int | None = None,
) -> Iterable[PlannedRun]:
    """Generate runs every `interval_hours` from interval_anchor + start_time.

    Two sub-modes:
      • Without `interval_end_time` (legacy): continuous across day
        boundaries — "every 6h starting 06:00" fires 06, 12, 18, 00,
        06, 12, ... until end_date / window end.
      • With `interval_end_time` (v1.14.1): daily window — re-anchors at
        `start_time` each day, stops when the next firing would exceed
        `interval_end_time` (inclusive), then resumes next day. So
        "every 2h from 06:00 to 20:00" fires 06, 08, 10, 12, 14, 16,
        18, 20 each day; nothing between 20:00 and 06:00.

    Skips runs before interval_anchor; stops at end_date if set.
    """
    tz = from_dt.tzinfo
    step_hours = sched.interval_hours
    anchor_dt = datetime.combine(sched.interval_anchor, sched.start_time, tzinfo=tz)
    step = timedelta(hours=step_hours)

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

    if sched.interval_end_time is None:
        # ── Continuous mode (legacy) ──
        current = anchor_dt
        if current < from_dt:
            elapsed = from_dt - anchor_dt
            n_steps = elapsed // step
            current = anchor_dt + n_steps * step
            if current < from_dt:
                current += step
        while current < end_dt:
            if is_within_active_window(sched, current.date()):
                for run in _expand_steps(sched, current, buffer_seconds):
                    if from_dt <= run.start_at < until_dt:
                        yield run
            current += step
        return

    # ── Daily-window mode ──
    # Iterate one day at a time over [from_dt.date, end_dt.date]. For each
    # day inside the active window AND >= interval_anchor, walk firings
    # from start_time until past interval_end_time.
    end_minutes = sched.interval_end_time.hour * 60 + sched.interval_end_time.minute
    start_minutes = sched.start_time.hour * 60 + sched.start_time.minute
    step_minutes = step_hours * 60

    cur_date = from_dt.date()
    last_date = end_dt.date()
    while cur_date <= last_date:
        if cur_date >= sched.interval_anchor and is_within_active_window(sched, cur_date):
            fire_minutes = start_minutes
            while fire_minutes <= end_minutes:
                fire_dt = datetime.combine(cur_date, sched.start_time, tzinfo=tz) + timedelta(
                    minutes=(fire_minutes - start_minutes)
                )
                if fire_dt >= from_dt and fire_dt < end_dt:
                    for run in _expand_steps(sched, fire_dt, buffer_seconds):
                        if from_dt <= run.start_at < until_dt:
                            yield run
                fire_minutes += step_minutes
        cur_date += timedelta(days=1)


def longest_run_span_minutes(scheds: Iterable[Schedule], zone_buffer_seconds: int | None) -> float:
    """Longest single firing span (minutes) across `scheds`, including the
    inter-zone valve buffers of multi-zone schedules.

    v1.20 — used to size the planning window so the conflict resolver can
    SEE a long run (e.g. a 5-hour Trees cycle) that started hours ago and
    is still physically running. With the old fixed 2-hour lookback the
    resolver was blind to those, so it would let another zone fire into a
    valve the controller still had open — a hardware collision.
    """
    buf_min = (
        DEFAULT_ZONE_BUFFER_SECONDS if zone_buffer_seconds is None else int(zone_buffer_seconds)
    ) / 60
    longest = 0.0
    for sched in scheds:
        steps = sched.all_steps()
        if not steps:
            continue
        span = sum(st.duration_minutes for st in steps) + buf_min * (len(steps) - 1)
        longest = max(longest, span)
    return longest


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
