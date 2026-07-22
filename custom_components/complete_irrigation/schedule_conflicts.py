"""Schedule conflict detection — pure logic, HA-free (v1.56).

The controller runs ONE zone at a time, so two runs whose time windows overlap
collide (unless a zone has its own independent water supply). This module finds
those collisions among a set of planned runs, tagged with each run's priority
(essential vs non-essential) so the caller can describe the conflict and decide
what to propose moving.

It does NOT resolve anything — the never-drop conflict_resolver remains the live
failsafe. This is the *detector* that fires the base check on add/edit and feeds
the LLM's rearrangement context. Pure: operates on run objects that expose
start_at (datetime), duration_minutes (int), zone_entity_id, schedule_id,
schedule_name, and essential (bool).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any


def _end_of(run: Any, buffer_minutes: int) -> Any:
    return run.start_at + timedelta(minutes=int(run.duration_minutes) + buffer_minutes)


def detect_conflicts(
    runs: list[Any],
    *,
    buffer_minutes: int = 0,
    independent_zones: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """Find pairs of runs that overlap on the shared controller.

    Each run occupies ``[start_at, start_at + duration + buffer)``. Two runs
    conflict when those windows overlap AND neither zone is independent (own
    supply). Returns a list of conflict dicts, earliest-first:

        {a, b, overlap_minutes, at, essential_involved, both_essential}

    where ``a``/``b`` are the two runs (a starts no later than b). Independent
    zones never appear. Buffer is the inter-run valve-settle gap.
    """
    indep = set(independent_zones or ())
    active = sorted(
        (r for r in runs if r.zone_entity_id not in indep),
        key=lambda r: r.start_at,
    )
    out: list[dict[str, Any]] = []
    for i, a in enumerate(active):
        a_end = _end_of(a, buffer_minutes)
        for b in active[i + 1 :]:
            if b.start_at >= a_end:
                break  # sorted by start: no later run can overlap `a` either
            # Two steps of the SAME schedule are sequential by construction (the
            # planner spaces them by the zone buffer) — never a real conflict.
            if a.schedule_id == b.schedule_id:
                continue
            # overlap window = [b.start_at, min(a_end, b_end))
            b_end = _end_of(b, buffer_minutes)
            overlap_end = min(a_end, b_end)
            overlap_min = max(0, int((overlap_end - b.start_at).total_seconds() // 60))
            if overlap_end <= b.start_at:
                continue
            out.append(
                {
                    "a": a,
                    "b": b,
                    "overlap_minutes": overlap_min,
                    "at": b.start_at,
                    "essential_involved": bool(
                        getattr(a, "essential", True) or getattr(b, "essential", True)
                    ),
                    "both_essential": bool(
                        getattr(a, "essential", True) and getattr(b, "essential", True)
                    ),
                }
            )
    return out


def conflicts_for_schedule(
    conflicts: list[dict[str, Any]], schedule_id: str
) -> list[dict[str, Any]]:
    """Filter detected conflicts to those involving a given schedule id (the newly
    added/edited one), so the add-check reports only what THIS change caused."""
    return [c for c in conflicts if schedule_id in (c["a"].schedule_id, c["b"].schedule_id)]


def summarize_conflict(c: dict[str, Any]) -> str:
    """A one-line, human-facing description of one conflict for a notification."""
    a, b = c["a"], c["b"]

    def _tag(run: Any) -> str:
        return "essential" if getattr(run, "essential", True) else "non-essential"

    when = c["at"].strftime("%a %H:%M")
    return (
        f"'{a.schedule_name}' ({_tag(a)}) and '{b.schedule_name}' ({_tag(b)}) "
        f"overlap by ~{c['overlap_minutes']} min around {when}"
    )
