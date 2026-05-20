"""Tests for ConflictResolver (pure logic).

Three policies (locked in by ADR-0001 from the P1 prototype):
  • DEFER_NEW       — keep first run, push second to start after first ends
  • SHIFT_EXISTING  — keep new run at requested time, move first earlier
  • SPLIT_DIFFERENCE — both yield equally to make room

Plus a 2-minute auto-buffer between back-to-back runs.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from custom_components.complete_irrigation.conflict_resolver import (
    POLICY_DEFER_NEW,
    POLICY_SHIFT_EXISTING_EARLIER,
    POLICY_SPLIT_DIFFERENCE,
    resolve_conflicts,
)
from custom_components.complete_irrigation.run_planner import PlannedRun

T = datetime(2026, 5, 18, 6, 0)  # Monday 6 AM


def _run(start_min_offset: int, duration: int, sid: str = "x") -> PlannedRun:
    return PlannedRun(
        zone_entity_id=f"switch.{sid}",
        start_at=T + timedelta(minutes=start_min_offset),
        duration_minutes=duration,
        schedule_id=sid,
        schedule_name=sid.upper(),
    )


def _times(runs):
    """Return (sid, offset_min, duration_min) tuples for compact assertions."""
    return [
        (
            r.schedule_id,
            int((r.start_at - T).total_seconds() // 60),
            r.duration_minutes,
        )
        for r in runs
    ]


# ── single run, no conflict ────────────────────────────────────────


def test_single_run_unchanged():
    runs = [_run(0, 30, "a")]
    assert _times(resolve_conflicts(runs, POLICY_DEFER_NEW)) == [("a", 0, 30)]


def test_non_overlapping_runs_unchanged():
    runs = [_run(0, 30, "a"), _run(60, 30, "b")]  # back-to-back gap of 30 min
    assert _times(resolve_conflicts(runs, POLICY_DEFER_NEW)) == [
        ("a", 0, 30),
        ("b", 60, 30),
    ]


# ── two-way overlap: policy A (defer new) ──────────────────────────


def test_defer_pushes_second_after_first_plus_buffer():
    # a: 0-30, b: 15-45 → b deferred to 32 (a end 30 + 2 buffer)
    runs = [_run(0, 30, "a"), _run(15, 30, "b")]
    out = resolve_conflicts(runs, POLICY_DEFER_NEW)
    assert _times(out) == [("a", 0, 30), ("b", 32, 30)]


# ── two-way overlap: policy B (shift existing earlier) ─────────────


def test_shift_existing_moves_first_earlier_by_overlap_plus_buffer():
    # a: 0-30, b: 15-45 → overlap 15 min, buffer 2 → shift first by 17 min
    # a' = -17 to 13, b stays at 15
    runs = [_run(0, 30, "a"), _run(15, 30, "b")]
    out = resolve_conflicts(runs, POLICY_SHIFT_EXISTING_EARLIER)
    assert _times(out) == [("a", -17, 30), ("b", 15, 30)]


# ── two-way overlap: policy C (split) ──────────────────────────────


def test_split_difference_shifts_both_roughly_half():
    # 15-min overlap + 2-min buffer = 17 total to absorb
    # Even split: 8 + 9 — bias smaller shift to existing (a moves -8, b moves +9)
    runs = [_run(0, 30, "a"), _run(15, 30, "b")]
    out = resolve_conflicts(runs, POLICY_SPLIT_DIFFERENCE)
    a, b = out
    # a should move earlier, b should move later
    assert a.start_at < T
    assert b.start_at > T + timedelta(minutes=15)
    # Sum of shifts >= overlap + buffer (17 min)
    a_shift = (T - a.start_at).total_seconds() / 60
    b_shift = (b.start_at - (T + timedelta(minutes=15))).total_seconds() / 60
    assert a_shift + b_shift >= 17


# ── back-to-back with auto-buffer ──────────────────────────────────


def test_zero_gap_gets_two_minute_buffer():
    # a: 0-30, b: 30-60 → b should be pushed to 32 to enforce 2-min gap
    runs = [_run(0, 30, "a"), _run(30, 30, "b")]
    out = resolve_conflicts(runs, POLICY_DEFER_NEW)
    assert _times(out) == [("a", 0, 30), ("b", 32, 30)]


# ── three-way cascade ──────────────────────────────────────────────


def test_three_way_cascade_resolves_iteratively():
    # All three overlapping. Defer policy → a stays, b deferred after a,
    # c deferred after (resolved) b.
    runs = [_run(0, 30, "a"), _run(10, 30, "b"), _run(20, 30, "c")]
    out = resolve_conflicts(runs, POLICY_DEFER_NEW)
    sids_and_starts = [(r.schedule_id, int((r.start_at - T).total_seconds() // 60)) for r in out]
    # Expected: a=0, b=32 (after a end 30 + 2 buffer), c=64 (after b end 62 + 2)
    assert sids_and_starts == [("a", 0), ("b", 32), ("c", 64)]


# ── cascade cap ────────────────────────────────────────────────────


def test_cascade_beyond_cap_marks_run_as_skipped():
    # 5 schedules all overlapping; with 60-min cap, eventually one must be
    # skipped rather than deferred indefinitely.
    runs = [_run(i * 5, 30, f"s{i}") for i in range(5)]
    out = resolve_conflicts(runs, POLICY_DEFER_NEW, cascade_cap_minutes=60)
    # Some runs should still be present (the early ones); at least one
    # should be marked as a skip reason.
    skipped = [r for r in out if r.reason.startswith("skipped:")]
    fired = [r for r in out if not r.reason.startswith("skipped:")]
    assert len(fired) >= 1
    assert len(skipped) >= 1


# ── empty input ────────────────────────────────────────────────────


def test_empty_input_returns_empty():
    assert resolve_conflicts([], POLICY_DEFER_NEW) == []
