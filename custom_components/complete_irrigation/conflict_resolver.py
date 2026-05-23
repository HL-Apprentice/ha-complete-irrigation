"""ConflictResolver — pure-logic enforcement of "one zone at a time".

Validated by the P1 prototype (see docs/ADRs/0001-conflict-resolver-verdict.md)
across two-way overlap, three-way cascade, back-to-back-with-buffer,
midnight crossings, and the cascade cap.

Three policies — see ADR for the worked examples:
  • DEFER_NEW              — keep earlier run, push later one
  • SHIFT_EXISTING_EARLIER — keep later run at requested time, move earlier one back
  • SPLIT_DIFFERENCE       — both yield ~half the overlap
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import timedelta

from .const import DEFAULT_BUFFER_MINUTES, DEFAULT_CASCADE_CAP_HOURS
from .run_planner import PlannedRun

POLICY_DEFER_NEW = "defer_new"
POLICY_SHIFT_EXISTING_EARLIER = "shift_existing"
POLICY_SPLIT_DIFFERENCE = "split_difference"

# Derived from const.py — single source of truth (v1.16 cleanup).
DEFAULT_CASCADE_CAP_MINUTES = DEFAULT_CASCADE_CAP_HOURS * 60
_MAX_ITERATIONS = 50


def resolve_conflicts(
    runs: list[PlannedRun],
    policy: str = POLICY_DEFER_NEW,
    buffer_minutes: int = DEFAULT_BUFFER_MINUTES,
    cascade_cap_minutes: int = DEFAULT_CASCADE_CAP_MINUTES,
) -> list[PlannedRun]:
    """Resolve all conflicts in `runs`, returning a list ordered by start time.

    Runs whose cumulative shift exceeds `cascade_cap_minutes` get a
    `reason` of `skipped: cascade-cap` instead of being deferred further.
    """
    if not runs:
        return []

    # Sort by start time; remember each run's original_start for cap.
    items = [(r, r.start_at) for r in sorted(runs, key=lambda r: r.start_at)]
    buffer = timedelta(minutes=buffer_minutes)
    cap = timedelta(minutes=cascade_cap_minutes)

    iterations = 0
    while iterations < _MAX_ITERATIONS:
        iterations += 1
        adjusted = False

        for i in range(len(items) - 1):
            first, first_orig = items[i]
            second, second_orig = items[i + 1]
            if _reason_is_skip(first) or _reason_is_skip(second):
                continue

            first_end = first.start_at + timedelta(minutes=first.duration_minutes)
            min_second_start = first_end + buffer

            if second.start_at >= min_second_start:
                continue  # no overlap (with buffer)

            overlap_with_buffer = (min_second_start - second.start_at).total_seconds() / 60

            if policy == POLICY_DEFER_NEW:
                items[i + 1] = _apply_defer(second, second_orig, min_second_start, cap)
                adjusted = True

            elif policy == POLICY_SHIFT_EXISTING_EARLIER:
                shift = timedelta(minutes=overlap_with_buffer)
                new_first_start = first.start_at - shift
                if (first_orig - new_first_start) > cap:
                    # Cap exceeded — fall back to deferring the second run.
                    items[i + 1] = _apply_defer(second, second_orig, min_second_start, cap)
                else:
                    items[i] = (
                        replace(first, start_at=new_first_start, reason="shifted-earlier"),
                        first_orig,
                    )
                adjusted = True

            elif policy == POLICY_SPLIT_DIFFERENCE:
                # Bias smaller shift to the existing (first) run.
                first_shift_min = math.floor(overlap_with_buffer / 2)
                second_shift_min = math.ceil(overlap_with_buffer / 2)
                new_first_start = first.start_at - timedelta(minutes=first_shift_min)
                new_second_start = second.start_at + timedelta(minutes=second_shift_min)
                if first_orig - new_first_start > cap or new_second_start - second_orig > cap:
                    # Either side exceeds cap — fall back to deferring second.
                    items[i + 1] = _apply_defer(second, second_orig, min_second_start, cap)
                else:
                    items[i] = (
                        replace(first, start_at=new_first_start, reason="split"),
                        first_orig,
                    )
                    items[i + 1] = (
                        replace(second, start_at=new_second_start, reason="split"),
                        second_orig,
                    )
                adjusted = True
            else:
                raise ValueError(f"unknown conflict policy: {policy}")

        # Re-sort after shifts (an earlier-shifted run may now be out of order)
        items.sort(key=lambda pair: pair[0].start_at)

        if not adjusted:
            break

    return [r for r, _ in items]


def _apply_defer(
    run: PlannedRun,
    original_start,
    min_start,
    cap: timedelta,
):
    """Push `run` to `min_start` if within the cascade cap, else mark
    it skipped. Shared by all three policies (the two non-defer policies
    fall back to this when their preferred shift exceeds the cap).

    Returns (PlannedRun, original_start_datetime) — the same tuple shape
    held in the `items` list inside resolve_conflicts."""
    if (min_start - original_start) > cap:
        return (_mark_skip(run, "cascade-cap"), original_start)
    return (replace(run, start_at=min_start, reason="deferred"), original_start)


def _reason_is_skip(run: PlannedRun) -> bool:
    return run.reason.startswith("skipped:")


def _mark_skip(run: PlannedRun, why: str) -> PlannedRun:
    return replace(run, reason=f"skipped:{why}")
