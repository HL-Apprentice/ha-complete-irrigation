"""Multi-tick simulation of the coordinator's plan->resolve->fire loop.

The coordinator recomputes the whole plan every 60s and fires via
due_runs_since((last, now]). A deferred/compressed run only fires if its
RESOLVED start lands inside one of those 60s windows. This test drives
that exact loop across a simulated day and asserts the bulletproof
guarantee: every scheduled firing fires EXACTLY once (never lost, never
doubled) and the single controller is never double-booked.

Pure logic — uses the real next_runs + resolve_conflicts + due_runs_since,
no HA. Mirrors ScheduleCoordinator._tick's planning core.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, time, timedelta
from itertools import pairwise

from custom_components.complete_irrigation.conflict_resolver import (
    DEFAULT_CASCADE_CAP_MINUTES,
    POLICY_DEFER_NEW,
    REASON_COMMITTED,
    resolve_conflicts,
)
from custom_components.complete_irrigation.const import (
    DEFAULT_BUFFER_MINUTES,
    DEFAULT_COMPRESS_FLOOR_PCT,
)
from custom_components.complete_irrigation.run_planner import (
    due_runs_since,
    longest_run_span_minutes,
    next_runs,
)
from custom_components.complete_irrigation.schedule import Schedule

DAY = datetime(2026, 6, 16, 0, 0, 0)  # a Tuesday


def _sched(sid, hh, mm, dur) -> Schedule:
    return Schedule(
        id=sid,
        name=sid,
        zone_entity_id=f"switch.{sid}",
        start_time=time(hh, mm),
        duration_minutes=dur,
        weekdays=(0, 1, 2, 3, 4, 5, 6),  # daily
    )


def simulate(scheds, start, end):
    """Run the coordinator's tick core minute-by-minute. Returns the list
    of fires: dicts with key, scheduled_start, fire_time, minutes."""
    fired: dict[tuple, dict] = {}  # key -> {start, minutes} (committed blockers)
    fires: list[dict] = []
    last = None
    now = start
    while now <= end:
        if last is None:
            last = now
            now += timedelta(minutes=1)
            continue

        # Lookahead covers the longest run (so a long run sees what it'll
        # collide with); lookback is generous (a full day) so a run deferred
        # behind a multi-hour backlog stays visible — both as a pending run
        # to fire and, once fired, as a committed blocker. The fired-set
        # excludes already-fired runs from being re-planned.
        horizon = timedelta(
            minutes=max(
                DEFAULT_CASCADE_CAP_MINUTES,
                longest_run_span_minutes(scheds, None) + DEFAULT_BUFFER_MINUTES,
            )
        )
        upcoming = next_runs(scheds, from_dt=last - timedelta(hours=24), until_dt=now + horizon)

        # Overlay already-fired runs as committed blockers (actual start+dur).
        overlaid = []
        for r in upcoming:
            key = (r.schedule_id, r.start_at.isoformat())
            if key in fired:
                info = fired[key]
                r = replace(
                    r,
                    start_at=info["start"],
                    duration_minutes=info["minutes"],
                    reason=REASON_COMMITTED,
                )
            overlaid.append(r)

        resolved = resolve_conflicts(
            overlaid,
            POLICY_DEFER_NEW,
            compress_floor_pct=DEFAULT_COMPRESS_FLOOR_PCT,
        )
        due = due_runs_since(resolved, last, now)
        for run in due:
            if run.reason == REASON_COMMITTED:
                continue  # already fired, just a blocker
            # key by the ORIGINAL scheduled firing (recover via scheduled_start_at)
            key = (run.schedule_id, run.scheduled_start_at.isoformat())
            if key in fired:
                continue  # defensive: never fire twice
            fired[key] = {"start": now, "minutes": run.duration_minutes}
            fires.append(
                {
                    "key": key,
                    "scheduled": run.scheduled_start_at,
                    "fire_time": now,
                    "minutes": run.duration_minutes,
                }
            )
        last = now
        now += timedelta(minutes=1)
    return fires


def _expected_firings(scheds, start, end):
    """Ground truth: every scheduled firing in [start, end], keyed."""
    runs = next_runs(scheds, from_dt=start, until_dt=end + timedelta(minutes=1))
    return {(r.schedule_id, r.start_at.isoformat()) for r in runs}


def test_no_run_is_ever_lost_under_giant_overlap():
    # A 4.5h giant straddles the morning; three small daily runs schedule
    # right into its shadow. Every one must still fire exactly once.
    scheds = [
        _sched("trees", 2, 0, 270),  # 02:00-06:30
        _sched("garden", 6, 0, 15),  # scheduled mid-giant
        _sched("grass", 6, 30, 20),  # scheduled at giant's tail
        _sched("citrus", 7, 30, 240),  # 7.5h... starts right after
    ]
    start = DAY + timedelta(hours=1)  # 01:00
    end = DAY + timedelta(hours=13)  # 13:00
    fires = simulate(scheds, start, end)

    fired_keys = {f["key"] for f in fires}
    expected = _expected_firings(scheds, start, end)

    missing = expected - fired_keys
    assert not missing, f"LOST runs (never fired): {sorted(missing)}"

    # exactly once each
    seen = [f["key"] for f in fires]
    assert len(seen) == len(set(seen)), "a run fired more than once"

    # controller never double-booked: order by fire time, each run's
    # [fire_time, fire_time+minutes) must not overlap the next fire.
    ordered = sorted(fires, key=lambda f: f["fire_time"])
    for a, b in pairwise(ordered):
        a_end = a["fire_time"] + timedelta(minutes=a["minutes"])
        assert b["fire_time"] >= a_end, (
            f"DOUBLE-BOOKED: {a['key']} runs {a['fire_time']}..{a_end}, "
            f"but {b['key']} fires {b['fire_time']}"
        )


def test_simple_daily_runs_all_fire_once():
    scheds = [_sched("a", 6, 0, 15), _sched("b", 8, 0, 20), _sched("c", 18, 0, 10)]
    start = DAY + timedelta(hours=5)
    end = DAY + timedelta(hours=19)
    fires = simulate(scheds, start, end)
    assert {f["key"] for f in fires} == _expected_firings(scheds, start, end)


def test_randomized_days_never_lose_a_run_or_double_book():
    """Fuzz: random feasible daily schedules through the full tick loop.
    Load is kept well under the day so every run has room — any lost run or
    double-booking is a real serialization bug, not just an out-of-time defer."""
    import random

    for seed in range(80):
        rng = random.Random(seed)
        n = rng.randint(2, 6)
        scheds = []
        for i in range(n):
            hh = rng.randint(2, 18)
            mm = rng.choice([0, 10, 15, 30, 45])
            dur = rng.choice([5, 10, 15, 20, 40, 90])
            scheds.append(_sched(f"z{i}", hh, mm, dur))
        start = DAY
        end = DAY + timedelta(hours=23, minutes=59)
        fires = simulate(scheds, start, end)
        ctx = (
            f"seed={seed} scheds={[(s.id, s.start_time.hour, s.duration_minutes) for s in scheds]}"
        )

        fired = {f["key"] for f in fires}
        expected = _expected_firings(scheds, start, end)
        assert fired == expected, f"lost/extra runs: {expected ^ fired} | {ctx}"

        ordered = sorted(fires, key=lambda f: f["fire_time"])
        for a, b in pairwise(ordered):
            a_end = a["fire_time"] + timedelta(minutes=a["minutes"])
            assert b["fire_time"] >= a_end, f"double-booked: {a['key']} vs {b['key']} | {ctx}"
