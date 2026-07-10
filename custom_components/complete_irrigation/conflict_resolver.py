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

import logging
import math
from dataclasses import replace
from datetime import datetime, timedelta

from .const import DEFAULT_BUFFER_MINUTES, DEFAULT_CASCADE_CAP_HOURS, DEFAULT_COMPRESS_FLOOR_PCT
from .gap_insertion import REASON_GAP_INSERTED
from .run_planner import PlannedRun

_LOGGER = logging.getLogger(__name__)

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

# v1.39 — reasons the resolver treats as FIXED intervals: kept exactly where
# they are (never deferred/compressed) and routed around by adjustable runs.
# REASON_GAP_INSERTED (gap_insertion.py) is a scheduled run pinned inside a
# chunked run's inter-block gap; unlike REASON_COMMITTED it has NOT fired
# yet, so the coordinator's fire loop keeps it fireable (its committed
# filter checks for REASON_COMMITTED specifically).
_FIXED_REASONS = (REASON_COMMITTED, REASON_GAP_INSERTED)

# How many minutes PAST its end a finished committed run stays a one-zone blocker:
# its inter-zone buffer PLUS this catch margin (>= one coordinator tick, TICK_SECONDS
# = 60s). The margin is load-bearing: the resolver defers a blocked run to
# (committed end + buffer); if the blocker vanished at exactly that instant the run's
# placement would snap back to its past scheduled time and the coordinator's half-open
# (last, now] window would never fire it (stranded). Holding the blocker a tick longer
# guarantees a tick fires the deferred run while it is still pinned in place — without
# clamping placements to `now` (which would re-fire every already-fired run each tick).
_BLOCKER_CATCH_MARGIN_MINUTES = 2


def committed_runs_from_sessions(
    sessions: dict,
    now: datetime,
    buffer_minutes: int = DEFAULT_BUFFER_MINUTES,
    independent_zones: frozenset[str] = frozenset(),
) -> list[PlannedRun]:
    """v1.32 — turn the live ``active_run_sessions`` registry (zone -> session dict,
    persisted by the run services) into ``REASON_COMMITTED`` PlannedRuns the resolver
    treats as fixed one-zone-at-a-time blockers. This is what makes a NEW scheduled
    run DEFER past an in-progress run (manual, scheduled, or resumed-after-restart)
    instead of firing a second zone into a controller that runs one zone at a time.

    Each session's busy interval is ``[started_at, deadline)`` — the gap-INCLUSIVE
    end, so a chunked run's inter-block gaps still count as controller-busy (we never
    slip a different zone into the gap between blocks). A session is dropped only once
    its inter-zone buffer PLUS a catch margin has elapsed
    (``deadline + buffer + catch_margin <= now``): the buffer makes a run scheduled
    right after a live one land ``buffer`` minutes later, and the catch margin (>= one
    tick) keeps the blocker alive a beat past the deferred run's slot so that run is
    fired before its blocker clears — never snapped back to its past scheduled time and
    lost. Sessions with missing / unparseable / tz-naive timestamps are skipped — fail
    safe: one bad record must never freeze all watering — but logged at WARNING,
    because a still-running zone silently losing its blocker is how two zones overlap.
    """
    out: list[PlannedRun] = []
    if not isinstance(sessions, dict):
        return out
    keep = timedelta(minutes=max(0, buffer_minutes) + _BLOCKER_CATCH_MARGIN_MINUTES)
    for zone, s in sessions.items():
        if not zone or not isinstance(s, dict):
            continue
        if zone in independent_zones:
            continue  # independent-supply zone (own water line) never blocks the controller
        try:
            started = datetime.fromisoformat(s["started_at"])
            deadline = datetime.fromisoformat(s.get("deadline") or s["water_deadline"])
        except (KeyError, TypeError, ValueError):
            _LOGGER.warning(
                "active session for %s has missing/unparseable timestamps; ignoring it "
                "as a one-zone blocker (watering overlap is possible if it is still "
                "running): %s",
                zone,
                s,
            )
            continue
        if started.tzinfo is None or deadline.tzinfo is None or now.tzinfo is None:
            _LOGGER.warning(
                "active session for %s has tz-naive timestamps; ignoring it as a "
                "one-zone blocker (watering overlap possible if it is still running)",
                zone,
            )
            continue
        if deadline + keep <= now:
            continue  # finished AND its inter-zone buffer elapsed — no longer a blocker
        out.append(
            PlannedRun(
                zone_entity_id=str(zone),
                start_at=started,
                duration_minutes=max(1, math.ceil((deadline - started).total_seconds() / 60)),
                schedule_id=s.get("schedule_id") or "",
                schedule_name=s.get("schedule_name") or "",
                reason=REASON_COMMITTED,
            )
        )
    return out


def stale_session_zones(
    sessions: dict, now: datetime, buffer_minutes: int = DEFAULT_BUFFER_MINUTES
) -> list[str]:
    """Zones whose run session is DEFINITIVELY finished — its deadline plus the same
    inter-zone buffer + catch margin that ``committed_runs_from_sessions`` uses has
    elapsed — so the record can be pruned from ``active_run_sessions``.

    This is the self-heal for an ORPHANED session: a chunked run interrupted before its
    final block (e.g. an HA restart mid-sequence) never clears its session, and the stale
    record then phantom-reserves the one-zone controller for its whole original window.
    Such a session is already ignored as a blocker (same threshold), so pruning it changes
    no scheduling decision — it only stops orphans accumulating and lingering across
    restarts. Conservative: a malformed / unparseable / tz-naive session is NOT reported
    (left in place, and already ignored as a blocker) so a merely-weird record is never
    dropped here.
    """
    out: list[str] = []
    if not isinstance(sessions, dict) or now.tzinfo is None:
        return out
    keep = timedelta(minutes=max(0, buffer_minutes) + _BLOCKER_CATCH_MARGIN_MINUTES)
    for zone, s in sessions.items():
        if not zone or not isinstance(s, dict):
            continue
        try:
            deadline = datetime.fromisoformat(s.get("deadline") or s["water_deadline"])
        except (KeyError, TypeError, ValueError):
            continue
        if deadline.tzinfo is None:
            continue
        if deadline + keep <= now:
            out.append(str(zone))
    return out


def resolve_conflicts(
    runs: list[PlannedRun],
    policy: str = POLICY_DEFER_NEW,
    buffer_minutes: int = DEFAULT_BUFFER_MINUTES,
    cascade_cap_minutes: int = DEFAULT_CASCADE_CAP_MINUTES,
    compress_floor_pct: int = DEFAULT_COMPRESS_FLOOR_PCT,
    independent_zones: frozenset[str] = frozenset(),
) -> list[PlannedRun]:
    """Serialize `runs` one-zone-at-a-time, ordered by start time.

    Never drops a run. See the module docstring for the strategy.

    A run whose `reason` is `REASON_COMMITTED` has already fired; it keeps
    its (actual) start and duration and acts only as a blocker — never
    moved, never compressed. The coordinator marks runs that way so an
    in-progress multi-hour run is respected. A `REASON_GAP_INSERTED` run
    (v1.39, gap_insertion.py) is fixed the same way — pinned inside a
    chunked run's inter-block gap — but has NOT fired yet: the coordinator
    still fires it (its post-resolve filter drops only REASON_COMMITTED).

    A run whose zone is in `independent_zones` is on its OWN water supply (e.g. a
    birdbath fill that does not share the drip manifold's pressure). It does not share
    the single controller, so it is pulled out of serialization entirely: it fires at
    its scheduled time, is never deferred/compressed by other zones, and never blocks
    them. This is the correct model for a zone that can safely run concurrently.
    """
    if not runs:
        return []

    # Independent-supply zones bypass one-zone-at-a-time completely.
    independent: list[PlannedRun] = []
    if independent_zones:
        independent = [r for r in runs if r.zone_entity_id in independent_zones]
        runs = [r for r in runs if r.zone_entity_id not in independent_zones]
        if not runs:
            return sorted(independent, key=lambda r: r.start_at)

    buffer = timedelta(minutes=buffer_minutes)
    cap_min = max(0, cascade_cap_minutes)
    floor_frac = max(0.0, min(1.0, compress_floor_pct / 100.0))

    # Committed runs are already firing: fixed [start, end+buffer) intervals
    # the controller is busy with. Adjustable runs must route AROUND them no
    # matter where they land in the sort order — a committed run that fired
    # late can sit later than an unfired run's scheduled time, so sorting
    # alone would mis-serialize and strand the unfired run (it would be
    # pinned in the past and never fire). Intervals fix that. v1.39 —
    # gap-inserted runs are fixed the same way: pinned inside a chunked
    # run's inter-block gap, never moved/compressed, routed around (their
    # interval sits inside the committed chunked span anyway).
    committed_runs = [r for r in runs if r.reason in _FIXED_REASONS]
    committed_intervals = sorted(
        (r.start_at, r.start_at + timedelta(minutes=int(r.duration_minutes)) + buffer)
        for r in committed_runs
    )

    # Adjustable runs, ordered by scheduled start.
    state: list[dict] = []
    for r in sorted((r for r in runs if r.reason not in _FIXED_REASONS), key=lambda r: r.start_at):
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
    return sorted(committed_runs + resolved + independent, key=lambda r: r.start_at)


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
