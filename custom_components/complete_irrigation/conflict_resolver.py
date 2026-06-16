"""ConflictResolver — pure-logic enforcement of "one zone at a time".

v1.20 — NEVER-DROP scheduling. Phoenix-summer requirement: a scheduled
run must never be silently dropped. When serializing runs one-zone-at-a-
time would push a run more than the cascade cap past its scheduled start,
the resolver first COMPRESSES the runs ahead of it — proportionally to
each one's slack, down to `compress_floor_pct`% of its requested minutes —
to reclaim time. Compression naturally lands on the long runs first (they
have the most slack), exactly as the user framed it ("pull the longer run
back by 10%"). If even full compression can't pull a run within the cap,
it is deferred *late* (and flagged for a notification) — but it still runs.

Runs already firing are marked `REASON_COMMITTED` by the caller (with their
actual start + minutes) and treated as fixed busy intervals: never moved,
never compressed, only routed around — so an in-progress multi-hour run is
respected. The `policy` argument is retained for backward compatibility;
resolution is now always defer-and-compress (we never run a zone *earlier*
than its scheduled time, which the old shift/split policies could do).

Validated by the P1 prototype (see docs/ADRs/0001-conflict-resolver-verdict.md)
for the serialization core; never-drop/compression added in v1.20.
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import timedelta

from .const import DEFAULT_BUFFER_MINUTES, DEFAULT_CASCADE_CAP_HOURS, DEFAULT_COMPRESS_FLOOR_PCT
from .run_planner import PlannedRun

POLICY_DEFER_NEW = "defer_new"
POLICY_SHIFT_EXISTING_EARLIER = "shift_existing"
POLICY_SPLIT_DIFFERENCE = "split_difference"

# Derived from const.py — single source of truth (v1.16 cleanup).
DEFAULT_CASCADE_CAP_MINUTES = DEFAULT_CASCADE_CAP_HOURS * 60

# Reason tags emitted on resolved runs. None start with "skipped:", so the
# coordinator fires every one of them (skip is no longer a resolver outcome).
REASON_SCHEDULED = "scheduled"
REASON_DEFERRED = "deferred"  # pushed later, within the cap, full duration
REASON_COMPRESSED = "compressed"  # duration shortened to make room
REASON_DEFERRED_LATE = "deferred-late"  # past the cap — alert-worthy, still runs
# A run already fired in a prior tick. The coordinator marks it with this
# reason and overlays its ACTUAL start + minutes; the resolver then treats
# it as a fixed blocker — never moved, never compressed — so in-progress
# long runs are respected without re-planning (and losing) deferred runs.
REASON_COMMITTED = "committed"


def resolve_conflicts(
    runs: list[PlannedRun],
    policy: str = POLICY_DEFER_NEW,
    buffer_minutes: int = DEFAULT_BUFFER_MINUTES,
    cascade_cap_minutes: int = DEFAULT_CASCADE_CAP_MINUTES,
    compress_floor_pct: int = DEFAULT_COMPRESS_FLOOR_PCT,
) -> list[PlannedRun]:
    """Serialize `runs` one-zone-at-a-time, ordered by start time.

    Never drops a run. See the module docstring for the strategy.

    A run whose `reason` is `REASON_COMMITTED` has already fired; it keeps
    its (actual) start and duration and acts only as a blocker — never
    moved, never compressed. The coordinator marks runs that way so an
    in-progress multi-hour run is respected.
    """
    if not runs:
        return []

    buffer = timedelta(minutes=buffer_minutes)
    cap_min = max(0, cascade_cap_minutes)
    floor_frac = max(0.0, min(1.0, compress_floor_pct / 100.0))

    # Committed runs are already firing: fixed [start, end+buffer) intervals
    # the controller is busy with. Adjustable runs must route AROUND them no
    # matter where they land in the sort order — a committed run that fired
    # late can sit later than an unfired run's scheduled time, so sorting
    # alone would mis-serialize and strand the unfired run (it would be
    # pinned in the past and never fire). Intervals fix that.
    committed_runs = [r for r in runs if r.reason == REASON_COMMITTED]
    committed_intervals = sorted(
        (r.start_at, r.start_at + timedelta(minutes=int(r.duration_minutes)) + buffer)
        for r in committed_runs
    )

    # Adjustable runs, ordered by scheduled start.
    state: list[dict] = []
    for r in sorted((r for r in runs if r.reason != REASON_COMMITTED), key=lambda r: r.start_at):
        full = int(r.duration_minutes)
        state.append(
            {
                "run": r,
                "sched": r.start_at,
                "start": r.start_at,
                "dur": full,
                "full": full,
                "floor": max(1, math.ceil(full * floor_frac)),
                "committed_bound": False,  # delay forced by an in-progress run
                "late_ok": False,  # loop-control: stop retrying this run
            }
        )

    def sweep() -> None:
        """Place each adjustable run at the earliest free slot at/after its
        scheduled time: past the previous run's end (buffered) and past any
        committed (in-progress) interval it would overlap."""
        cursor = None
        for s in state:
            base = s["sched"] if cursor is None else max(s["sched"], cursor)
            placed = _push_past_committed(base, s["dur"], committed_intervals, buffer)
            s["committed_bound"] = placed > base
            s["start"] = placed
            cursor = placed + timedelta(minutes=s["dur"]) + buffer

    iterations = 0
    max_iterations = max(50, len(state) * 4)
    while iterations < max_iterations:
        iterations += 1
        sweep()

        # Find the earliest run pushed past the cap that we haven't given up on.
        target = None
        for i, s in enumerate(state):
            if s["late_ok"]:
                continue
            late_min = (s["start"] - s["sched"]).total_seconds() / 60
            if late_min > cap_min:
                target = i
                target_late = late_min
                break
        if target is None:
            break

        # If the delay is forced by an in-progress committed run, compressing
        # earlier adjustable runs can't pull it in — accept it late (never drop).
        if state[target]["committed_bound"]:
            state[target]["late_ok"] = True
            continue

        need = target_late - cap_min  # minutes of lateness to reclaim
        # Reclaim by compressing the adjustable runs ahead of `target`.
        # Greedy: shave one minute at a time off whichever predecessor has
        # the most remaining slack, so the long runs absorb the squeeze.
        reclaimed = _compress_predecessors(state[:target], math.ceil(need))
        if reclaimed <= 0:
            # No slack left anywhere ahead of it — let it run late, never drop.
            state[target]["late_ok"] = True
        # else: loop re-sweeps with the shortened predecessors.

    sweep()

    resolved = [
        replace(
            s["run"],
            start_at=s["start"],
            duration_minutes=s["dur"],
            reason=_reason_for(s, cap_min),
        )
        for s in state
    ]
    return sorted(committed_runs + resolved, key=lambda r: r.start_at)


def _push_past_committed(start, dur, intervals, buffer):
    """Advance `start` past any committed interval the run [start, start+dur)
    would overlap. Intervals are (start, end+buffer); pushing lands the run
    exactly at a committed run's buffered end. Re-checks until clear."""
    run_len = timedelta(minutes=dur)
    moved = True
    while moved:
        moved = False
        for cs, ce in intervals:
            if start < ce and start + run_len > cs:
                start = ce
                moved = True
    return start


def _compress_predecessors(preds: list[dict], need_minutes: int) -> int:
    """Shave up to `need_minutes` total off the adjustable predecessors,
    one minute at a time from whichever has the most remaining slack.

    Compresses the long runs first (they hold the most slack), matching
    the user's "pull the longer run back" intent. Returns minutes reclaimed.
    """
    reclaimed = 0
    while reclaimed < need_minutes:
        best = None
        best_slack = 0
        for p in preds:
            slack = p["dur"] - p["floor"]
            if slack > best_slack:
                best_slack = slack
                best = p
        if best is None or best_slack <= 0:
            break  # nothing left to give
        best["dur"] -= 1
        reclaimed += 1
    return reclaimed


def _reason_for(s: dict, cap_min: int) -> str:
    """Tag describing how this adjustable run was resolved (drives alerts)."""
    late_min = (s["start"] - s["sched"]).total_seconds() / 60
    if late_min > cap_min:
        return REASON_DEFERRED_LATE
    if s["dur"] < s["full"]:
        return REASON_COMPRESSED
    if late_min > 0:
        return REASON_DEFERRED
    return s["run"].reason or REASON_SCHEDULED
