"""Tests for ConflictResolver (pure logic).

v1.20 — NEVER-DROP scheduling. The resolver serializes one-zone-at-a-time
and, when a run would be pushed past the cascade cap, COMPRESSES the runs
ahead of it (down to a floor %) to reclaim time. A run is never dropped —
at worst it runs late (reason 'deferred-late'). Runs are never moved
*earlier* than scheduled, so the legacy shift/split policies now behave
like defer + compress (the `policy` arg is retained for compatibility).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from itertools import pairwise

from custom_components.complete_irrigation.conflict_resolver import (
    POLICY_DEFER_NEW,
    POLICY_SPLIT_DIFFERENCE,
    REASON_COMMITTED,
    REASON_COMPRESSED,
    REASON_DEFERRED,
    REASON_DEFERRED_LATE,
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


def _by_id(runs):
    return {r.schedule_id: r for r in runs}


# ── no conflict ────────────────────────────────────────────────────


def test_single_run_unchanged():
    runs = [_run(0, 30, "a")]
    assert _times(resolve_conflicts(runs, POLICY_DEFER_NEW)) == [("a", 0, 30)]


def test_non_overlapping_runs_unchanged():
    runs = [_run(0, 30, "a"), _run(60, 30, "b")]  # back-to-back gap of 30 min
    assert _times(resolve_conflicts(runs, POLICY_DEFER_NEW)) == [
        ("a", 0, 30),
        ("b", 60, 30),
    ]


def test_empty_input_returns_empty():
    assert resolve_conflicts([], POLICY_DEFER_NEW) == []


# ── two-way overlap: defer the later run, never earlier ────────────


def test_defer_pushes_second_after_first_plus_buffer():
    # a: 0-30, b: 15-45 → b deferred to 32 (a end 30 + 2 buffer)
    runs = [_run(0, 30, "a"), _run(15, 30, "b")]
    out = resolve_conflicts(runs, POLICY_DEFER_NEW)
    assert _times(out) == [("a", 0, 30), ("b", 32, 30)]
    assert _by_id(out)["b"].reason == REASON_DEFERRED


def test_zero_gap_gets_two_minute_buffer():
    # a: 0-30, b: 30-60 → b should be pushed to 32 to enforce 2-min gap
    runs = [_run(0, 30, "a"), _run(30, 30, "b")]
    out = resolve_conflicts(runs, POLICY_DEFER_NEW)
    assert _times(out) == [("a", 0, 30), ("b", 32, 30)]


def test_legacy_split_policy_never_runs_earlier_than_scheduled():
    # The old SPLIT_DIFFERENCE moved a run earlier; v1.20 never does.
    runs = [_run(0, 30, "a"), _run(15, 30, "b")]
    out = resolve_conflicts(runs, POLICY_SPLIT_DIFFERENCE)
    for r in out:
        assert r.start_at >= _by_id([_run(0, 30, "a"), _run(15, 30, "b")])[r.schedule_id].start_at


# ── three-way cascade ──────────────────────────────────────────────


def test_three_way_cascade_resolves_iteratively():
    runs = [_run(0, 30, "a"), _run(10, 30, "b"), _run(20, 30, "c")]
    out = resolve_conflicts(runs, POLICY_DEFER_NEW)
    starts = [(r.schedule_id, int((r.start_at - T).total_seconds() // 60)) for r in out]
    # a=0, b=32 (after a end 30 + 2 buffer), c=64 (after b end 62 + 2)
    assert starts == [("a", 0), ("b", 32), ("c", 64)]


# ── never-drop: the core v1.20 guarantee ───────────────────────────


def test_never_drops_a_run_even_under_heavy_contention():
    # 5 schedules all overlapping with a tight 60-min cap. Old behavior
    # dropped at least one (skipped:cascade-cap). v1.20 keeps them all.
    runs = [_run(i * 5, 30, f"s{i}") for i in range(5)]
    out = resolve_conflicts(runs, POLICY_DEFER_NEW, cascade_cap_minutes=60)
    assert len(out) == 5
    assert all(not r.reason.startswith("skipped:") for r in out)
    # Serialization still holds: no two runs overlap (buffer respected).
    ordered = sorted(out, key=lambda r: r.start_at)
    for a, b in pairwise(ordered):
        assert b.start_at >= a.start_at + timedelta(minutes=a.duration_minutes)


# ── compress-to-fit ────────────────────────────────────────────────


def test_compresses_long_predecessor_to_pull_run_within_cap():
    # Long run A (300 min) blocks small run B; without compression B would
    # land 92 min late (> 60 cap). Compressing A reclaims the slack so B
    # lands within the cap, and A stays >= its 70% floor (210 min).
    runs = [_run(0, 300, "A"), _run(210, 10, "B")]
    out = resolve_conflicts(runs, POLICY_DEFER_NEW, cascade_cap_minutes=60, compress_floor_pct=70)
    by = _by_id(out)
    assert by["A"].duration_minutes < 300  # compressed
    assert by["A"].duration_minutes >= 210  # never below 70% floor
    assert by["A"].reason == REASON_COMPRESSED
    b_late = (by["B"].start_at - (T + timedelta(minutes=210))).total_seconds() / 60
    assert b_late <= 60  # pulled back within the cap


def test_compression_never_breaches_the_floor():
    # Demand more reclaim than the floor allows: A can only give 30% (90 of
    # 300). B stays late, A bottoms out exactly at the floor, nothing drops.
    runs = [_run(0, 300, "A"), _run(10, 10, "B")]
    out = resolve_conflicts(runs, POLICY_DEFER_NEW, cascade_cap_minutes=60, compress_floor_pct=70)
    by = _by_id(out)
    assert by["A"].duration_minutes == 210  # floor = ceil(300 * 0.70)
    assert by["B"].reason == REASON_DEFERRED_LATE  # past cap, but present
    assert len(out) == 2


def test_deferred_late_still_runs_when_no_slack():
    # Two short runs, tiny cap, nothing to compress (already near floor).
    # The squeezed run is flagged deferred-late but is NOT dropped.
    runs = [_run(0, 30, "a"), _run(1, 30, "b")]
    out = resolve_conflicts(runs, POLICY_DEFER_NEW, cascade_cap_minutes=5)
    by = _by_id(out)
    assert len(out) == 2
    assert by["b"].reason == REASON_DEFERRED_LATE


# ── committed (in-progress) runs ───────────────────────────────────


def test_committed_run_is_never_moved_or_compressed():
    # A is already running (reason=committed): keep its start and full
    # duration, use it only as a blocker. B defers around it.
    committed_a = replace(_run(0, 300, "A"), reason=REASON_COMMITTED)
    runs = [committed_a, _run(10, 10, "B")]
    out = resolve_conflicts(runs, POLICY_DEFER_NEW, cascade_cap_minutes=60, compress_floor_pct=70)
    by = _by_id(out)
    assert by["A"].duration_minutes == 300  # untouched
    assert by["A"].reason == REASON_COMMITTED
    assert int((by["A"].start_at - T).total_seconds() // 60) == 0
    assert by["B"].start_at >= T + timedelta(minutes=300)  # waits for A
    assert by["B"].reason == REASON_DEFERRED_LATE
    assert len(out) == 2


# ── stranded-run rescue (v1.39) ────────────────────────────────────
#
# The coordinator fires via the half-open (last_tick, now] window, so an
# UNFIRED adjustable run whose placement lands at/before last_tick can never
# fire — stranded. With rescue_stranded_* set, the resolver re-places such a
# run no earlier than rescue_stranded_at (= now), routed past committed
# intervals. The caller guarantees fired occurrences never reach the resolver
# (the coordinator's fired-occurrence ledger), so rescue can't double-fire.


def test_no_rescue_by_default_past_placement_is_preserved():
    # Preview callers (calendar/ical/ws_api) pass no rescue kwargs: a run
    # scheduled in the past with no blockers stays at its past time.
    out = resolve_conflicts([_run(0, 20, "a")], POLICY_DEFER_NEW)
    assert out[0].start_at == T  # untouched, even though "in the past"


def test_stranded_run_is_rescued_to_now_and_routed_past_committed():
    # `a` was scheduled at T and never fired; now = T+120 and a committed run
    # occupies [T+115, T+145). Rescue must pull `a` to now and then past the
    # committed interval's buffered end — never fire it into a live run.
    now = T + timedelta(minutes=120)
    last = now - timedelta(minutes=1)
    committed = replace(_run(115, 30, "L"), reason=REASON_COMMITTED)
    out = resolve_conflicts(
        [committed, _run(0, 20, "a")],
        POLICY_DEFER_NEW,
        buffer_minutes=2,
        rescue_stranded_before=last,
        rescue_stranded_at=now,
    )
    by = _by_id(out)
    assert by["a"].start_at == T + timedelta(minutes=145 + 2)  # committed end + buffer
    assert by["a"].reason == REASON_DEFERRED_LATE  # hours late -> alert-worthy
    assert by["a"].scheduled_start_at == T  # occurrence identity preserved


def test_stranded_run_with_clear_controller_fires_at_now():
    now = T + timedelta(minutes=90)
    out = resolve_conflicts(
        [_run(0, 20, "a")],
        POLICY_DEFER_NEW,
        rescue_stranded_before=now - timedelta(minutes=1),
        rescue_stranded_at=now,
    )
    assert out[0].start_at == now  # lands exactly in (last, now] -> fires this tick


def test_rescue_ineligible_run_stays_stranded():
    # The coordinator gates rescue to occurrences it was actively deferring
    # (never backfire lockout- or downtime-missed runs). Ineligible -> as-is.
    now = T + timedelta(minutes=90)
    out = resolve_conflicts(
        [_run(0, 20, "a")],
        POLICY_DEFER_NEW,
        rescue_stranded_before=now - timedelta(minutes=1),
        rescue_stranded_at=now,
        rescue_eligible=lambda r: False,
    )
    assert out[0].start_at == T  # left stranded, exactly as before the fix


def test_two_stranded_runs_rescue_serialized_one_zone_at_a_time():
    now = T + timedelta(minutes=90)
    out = resolve_conflicts(
        [_run(0, 20, "a"), _run(5, 10, "b")],
        POLICY_DEFER_NEW,
        buffer_minutes=2,
        rescue_stranded_before=now - timedelta(minutes=1),
        rescue_stranded_at=now,
    )
    by = _by_id(out)
    assert by["a"].start_at == now
    assert by["b"].start_at == now + timedelta(minutes=20 + 2)  # after a + buffer


def test_rescue_leaves_future_runs_untouched():
    # A future-scheduled run must keep its exact placement and reason even
    # when rescue is armed (rescue only touches placements <= last).
    now = T - timedelta(minutes=30)
    out = resolve_conflicts(
        [_run(0, 20, "a")],
        POLICY_DEFER_NEW,
        rescue_stranded_before=now - timedelta(minutes=1),
        rescue_stranded_at=now,
    )
    assert out[0].start_at == T
    assert out[0].reason == "scheduled"
