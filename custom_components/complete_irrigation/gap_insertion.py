"""Gap-aware short-run insertion — pure logic, HA-free.

v1.39 — a run longer than the controller's per-activation cap is delivered as
back-to-back BLOCKS with an inter-block gap (chunked_run.plan_blocks +
services._dispatch_run_blocks). The conflict resolver treats the whole chunked
span — gaps included — as controller-busy (committed_runs_from_sessions uses
the gap-inclusive deadline), so a short scheduled run on the shared manifold
used to defer for HOURS behind a 5-block run even though the controller is
genuinely IDLE during every gap.

This module decides when a short SCHEDULED run fits ENTIRELY inside one
inter-block gap and pins it there instead. Hard invariants:

  * One-zone-at-a-time is inviolable. An insertion must fit inside ONE gap
    with a safety margin on BOTH sides — it never overlaps a block, even when
    the coordinator fires it up to one tick late (see margin below).
  * The long run's cadence never shifts. Blocks are timer-driven
    (services._dispatch_run_blocks set them at dispatch); this module only
    READS the block plan recorded on the session — it never moves a block.
  * Only SCHEDULED runs are inserted. Manual runs are invisible to the
    planner (they only ever appear as committed sessions), and a candidate
    must carry the planner's plain "scheduled" reason.
  * A run that fits no gap is left untouched — it defers exactly as before.
  * Deterministic and pure: a pinned start depends only on the session's
    recorded block plan, the run's scheduled start, and config — never on
    "now" — so every tick recomputes the SAME pin until the coordinator's
    due window (last, now] catches and fires it.

MARGIN — how much clearance each side of the inserted run needs:
``effective_margin_seconds`` = the existing ``zone_buffer_seconds`` config
(the inter-zone valve-settle buffer, default schedule.DEFAULT_ZONE_BUFFER_
SECONDS) PLUS one coordinator tick (DEFAULT_FIRE_LATENCY_SECONDS = 60s,
matching coordinator.TICK_SECONDS). The tick term is load-bearing:
due_runs_since fires a run up to one tick AFTER its pinned start, so the
actual valve-open can drift a full tick late — the tail margin must absorb
that drift or the run's auto-stop could land inside the next block. A gap
therefore hosts a run only when

    gap_seconds >= run_minutes * 60 + 2 * effective_margin_seconds

and the pinned start is ``gap.start + margin`` (or the run's scheduled start,
whichever is later — a run is never fired EARLIER than scheduled).

The block plan is read from the session record's ``blocks`` +
``block_gap_seconds`` keys, written by services.handle_run_zone at dispatch
time — the exact numbers its block timers were armed with. A session without
a recorded plan (a single-activation run, or one written by an older version)
simply exposes no gaps: fail safe, no insertion.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from .const import DEFAULT_BUFFER_MINUTES
from .run_planner import PlannedRun
from .schedule import DEFAULT_ZONE_BUFFER_SECONDS

# Reason tag for a run pinned into a gap. The resolver treats it like
# REASON_COMMITTED (a FIXED interval — never moved, never compressed, other
# runs route around it), but the coordinator still FIRES it (its committed
# filter checks for REASON_COMMITTED specifically).
REASON_GAP_INSERTED = "gap-inserted"

# Fire latency allowance (seconds) — one coordinator tick. due_runs_since
# catches a pinned start up to one tick after it passes, so the margin must
# cover that drift. Kept as a module constant (this module must stay HA-free
# and cannot import coordinator.TICK_SECONDS); the coordinator passes its
# real TICK_SECONDS in explicitly.
DEFAULT_FIRE_LATENCY_SECONDS = 60

# The planner's default reason on a fresh PlannedRun — only these runs are
# insertion candidates (mirrors conflict_resolver.REASON_SCHEDULED, which we
# can't import without creating an import cycle).
_REASON_SCHEDULED = "scheduled"


@dataclass(frozen=True)
class GapWindow:
    """One controller-idle window between two blocks of a chunked run."""

    zone_entity_id: str  # the long run's (owner's) zone
    start: datetime  # previous block's end — valve just closed
    end: datetime  # next block's start — valve re-opens

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()


def effective_margin_seconds(
    zone_buffer_seconds: int | None = None,
    tick_seconds: int = DEFAULT_FIRE_LATENCY_SECONDS,
) -> int:
    """Safety clearance each side of an inserted run: the inter-zone
    valve-settle buffer (zone_buffer_seconds config, defaulting to
    DEFAULT_ZONE_BUFFER_SECONDS) + one tick of fire latency."""
    zb = (
        DEFAULT_ZONE_BUFFER_SECONDS
        if zone_buffer_seconds is None
        else max(0, int(zone_buffer_seconds))
    )
    return zb + max(0, int(tick_seconds))


def session_gap_windows(zone: str, session: dict) -> list[GapWindow]:
    """Reconstruct the inter-block gap windows of a chunked session from its
    recorded block plan (``blocks`` + ``block_gap_seconds`` + ``started_at``).

    Mirrors services._dispatch_run_blocks exactly: block i starts at
    started_at + sum(blocks[:i]) minutes + i gaps; the gap after block i runs
    from that block's end for block_gap_seconds. Returns [] for anything that
    is not a well-formed chunked session (single-activation run, missing/
    malformed plan, unparseable timestamp) — fail safe: no plan, no insertion.
    """
    if not isinstance(session, dict):
        return []
    blocks = session.get("blocks")
    gap_s = session.get("block_gap_seconds")
    if not isinstance(blocks, list) or len(blocks) < 2:
        return []
    if not all(isinstance(b, int | float) and not isinstance(b, bool) and b > 0 for b in blocks):
        return []
    if not isinstance(gap_s, int | float) or isinstance(gap_s, bool) or gap_s <= 0:
        return []
    try:
        cursor = datetime.fromisoformat(str(session["started_at"]))
    except (KeyError, TypeError, ValueError):
        return []
    gap_len = timedelta(seconds=float(gap_s))
    gaps: list[GapWindow] = []
    for b in blocks[:-1]:  # no gap after the final block
        block_end = cursor + timedelta(minutes=float(b))
        gaps.append(GapWindow(zone_entity_id=zone, start=block_end, end=block_end + gap_len))
        cursor = block_end + gap_len
    return gaps


def insertion_start_in_gap(
    gap: GapWindow,
    earliest_start: datetime,
    run_len: timedelta,
    margin: timedelta,
) -> datetime | None:
    """The pinned start for a run of ``run_len`` inside ``gap``, or None when
    it does not fit. The run starts margin past the gap's opening (never
    before ``earliest_start`` — a run is never fired earlier than scheduled)
    and must end at least margin before the gap closes:

        [gap.start … margin … run … margin … gap.end]

    A gap of exactly run + 2*margin fits; one second less does not.
    """
    start = gap.start + margin
    if earliest_start > start:
        start = earliest_start
    if start + run_len + margin <= gap.end:
        return start
    return None


def insertion_key(run: PlannedRun) -> str:
    """Stable identity of one scheduled occurrence — the key for both the
    insertion plan and the coordinator's fired ledger. Uses the ORIGINAL
    scheduled start (scheduled_start_at survives conflict-resolution and
    dataclasses.replace), so a pinned/deferred copy keys identically."""
    sched = run.scheduled_start_at or run.start_at
    return f"{run.schedule_id}|{run.zone_entity_id}|{sched.isoformat()}"


def plan_gap_insertions(
    runs: Iterable[PlannedRun],
    sessions: dict,
    now: datetime,
    *,
    zone_buffer_seconds: int | None = None,
    buffer_minutes: int = DEFAULT_BUFFER_MINUTES,
    tick_seconds: int = DEFAULT_FIRE_LATENCY_SECONDS,
    independent_zones: frozenset[str] = frozenset(),
    fired_keys: Collection[str] = frozenset(),
    controller_cap_minutes: int | None = None,
) -> dict[str, datetime]:
    """Select which scheduled runs to pin into which inter-block gaps.

    Returns {insertion_key(run): pinned_start} for every run that fits a gap.
    The caller (coordinator._tick) rewrites those runs' start_at to the pin
    and tags them REASON_GAP_INSERTED; everything else resolves exactly as
    before (zero regression for runs that fit no gap).

    Selection rules (all must hold):
      * the run is a plain scheduled candidate: reason == "scheduled", not an
        independent-supply zone (those never defer anyway), positive duration,
        not already fired via insertion (``fired_keys`` — the coordinator's
        ledger; without it the occurrence would be re-inserted into the NEXT
        gap after firing, because its scheduled start stays blocked by the
        still-running long session, and watered twice);
      * the run is not longer than ``controller_cap_minutes`` (when given):
        the fit math below models ONE continuous activation, but dispatch
        funnels through run_zone, which CHUNKS anything over the controller
        cap into blocks separated by block_gap_seconds — wall time that
        bursts out of the host gap into the long run's next block. A
        would-chunk run is never pinned; it defers exactly as before;
      * the run's own zone has no live session (its occurrence IS that
        session), and its scheduled interval actually collides with a live
        shared-line session — an unblocked run is left to fire on time;
      * the pinned start fits one gap with margin both sides (see module
        docstring), is not earlier than the scheduled start, and is still
        catchable — ``pin > now - tick_seconds`` — so a gap whose slot has
        already slipped past the due window is skipped (the run then simply
        defers as today, or takes a later gap);
      * the inserted interval overlaps no OTHER session's busy span (the
        owner's span necessarily contains its own gaps and is exempt);
      * one run per gap — two shorts never share a gap — and gaps are tried
        earliest-first for the earliest-scheduled run first (deterministic).

    Fail-safe posture throughout: malformed sessions, tz-naive/aware
    mismatches, or missing data yield NO insertion (the run defers as today).
    """
    out: dict[str, datetime] = {}
    if not isinstance(sessions, dict) or not sessions:
        return out

    def _mismatched(dt: datetime) -> bool:
        return (dt.tzinfo is None) != (now.tzinfo is None)

    margin = timedelta(seconds=effective_margin_seconds(zone_buffer_seconds, tick_seconds))
    buffer = timedelta(minutes=max(0, buffer_minutes))
    catch = timedelta(seconds=max(0, int(tick_seconds)))

    # Live shared-line sessions -> busy spans [started, deadline + buffer)
    # (the same gap-INCLUSIVE interval committed_runs_from_sessions blocks
    # with) + every recorded inter-block gap window.
    busy: list[tuple[str, datetime, datetime]] = []
    gaps: list[GapWindow] = []
    for zone, s in sessions.items():
        if not zone or not isinstance(s, dict) or zone in independent_zones:
            continue
        try:
            started = datetime.fromisoformat(str(s["started_at"]))
            deadline = datetime.fromisoformat(str(s.get("deadline") or s["water_deadline"]))
        except (KeyError, TypeError, ValueError):
            continue  # fail safe: never insert against a malformed session
        if _mismatched(started) or _mismatched(deadline):
            continue
        busy.append((str(zone), started, deadline + buffer))
        gaps.extend(session_gap_windows(str(zone), s))
    if not gaps:
        return out
    gaps.sort(key=lambda g: (g.start, g.zone_entity_id))

    session_zones = {z for z, _, _ in busy}
    taken: set[int] = set()
    for run in sorted(runs, key=lambda r: (r.start_at, r.zone_entity_id, r.schedule_id)):
        if run.reason != _REASON_SCHEDULED:
            continue
        if run.zone_entity_id in independent_zones or run.zone_entity_id in session_zones:
            continue
        if int(run.duration_minutes) <= 0 or _mismatched(run.start_at):
            continue
        if controller_cap_minutes is not None and int(run.duration_minutes) > int(
            controller_cap_minutes
        ):
            continue  # would be CHUNKED at dispatch — bursts the host gap (see docstring)
        key = insertion_key(run)
        if key in fired_keys or key in out:
            continue
        run_len = timedelta(minutes=int(run.duration_minutes))
        sched = run.start_at
        # Only runs actually blocked by a live session are candidates —
        # the same overlap test the resolver's _push_past_committed uses.
        if not any(sched < end and sched + run_len > start for _, start, end in busy):
            continue
        for i, gap in enumerate(gaps):
            if i in taken or gap.zone_entity_id == run.zone_entity_id:
                continue
            pin = insertion_start_in_gap(gap, sched, run_len, margin)
            if pin is None:
                continue
            if pin <= now - catch:
                continue  # slot already slipped past the due window
            # Never overlap another zone's live run (the gap owner's span
            # contains its own gaps by construction and is exempt).
            if any(
                z != gap.zone_entity_id and pin < end and pin + run_len > start
                for z, start, end in busy
            ):
                continue
            out[key] = pin
            taken.add(i)
            break
    return out
