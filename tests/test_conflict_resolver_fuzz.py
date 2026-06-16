"""Property-based fuzz of the never-drop resolver (v1.20).

Hammers resolve_conflicts with thousands of randomized scenarios and
asserts the safety invariants that the Phoenix-summer requirement rests
on. Any violation prints a reproducible seed.

Invariants:
  1. NEVER drops — output count == input count, no 'skipped:' reasons.
  2. NEVER early — no run starts before its scheduled time.
  3. Floor respected — adjustable runs stay in [floor, full]; committed
     runs keep full duration.
  4. Committed pinned — a committed run keeps its scheduled start + full
     duration (used only as a blocker).
  5. Serialized — every adjustable run starts at/after the previous
     output run's end (one-zone-at-a-time for the runs we control).
  6. Terminates — the call returns (no hang) for every scenario.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta

from custom_components.complete_irrigation.conflict_resolver import (
    POLICY_DEFER_NEW,
    REASON_COMMITTED,
    resolve_conflicts,
)
from custom_components.complete_irrigation.run_planner import PlannedRun

T = datetime(2026, 6, 16, 3, 0)
SCENARIOS = 4000


def _make_scenario(rng):
    n = rng.randint(1, 8)
    runs = []
    for i in range(n):
        # ~1 in 4 runs is already firing (committed blocker).
        reason = REASON_COMMITTED if rng.random() < 0.25 else "scheduled"
        runs.append(
            PlannedRun(
                zone_entity_id=f"switch.z{i}",
                start_at=T + timedelta(minutes=rng.randint(0, 600)),
                duration_minutes=rng.randint(1, 320),
                schedule_id=f"s{i}",
                schedule_name=f"S{i}",
                reason=reason,
            )
        )
    cap = rng.choice([0, 5, 30, 60, 120, 180])
    floor_pct = rng.choice([0, 50, 70, 85, 100])
    buffer = rng.choice([0, 2, 5])
    return runs, cap, floor_pct, buffer


def test_resolver_invariants_fuzz():
    for seed in range(SCENARIOS):
        rng = random.Random(seed)
        runs, cap, floor_pct, buffer = _make_scenario(rng)
        ctx = f"seed={seed} cap={cap} floor={floor_pct} buf={buffer}"

        out = resolve_conflicts(
            runs,
            POLICY_DEFER_NEW,
            buffer_minutes=buffer,
            cascade_cap_minutes=cap,
            compress_floor_pct=floor_pct,
        )

        # 1. never drops
        assert len(out) == len(runs), f"DROPPED a run: {ctx}"
        assert all(not r.reason.startswith("skipped:") for r in out), f"skipped reason: {ctx}"

        sched_by_id = {r.schedule_id: r for r in runs}
        floor_frac = max(0.0, min(1.0, floor_pct / 100.0))
        for r in out:
            orig = sched_by_id[r.schedule_id]
            is_committed = orig.reason == REASON_COMMITTED
            full = orig.duration_minutes
            # 2. never earlier than scheduled
            assert r.start_at >= orig.start_at, f"ran EARLY: {r.schedule_id} {ctx}"
            if is_committed:
                # 4. committed pinned
                assert r.start_at == orig.start_at, f"committed moved: {r.schedule_id} {ctx}"
                assert r.duration_minutes == full, f"committed compressed: {r.schedule_id} {ctx}"
            else:
                # 3. floor respected
                floor = max(1, math.ceil(full * floor_frac))
                assert floor <= r.duration_minutes <= full, (
                    f"floor breach: {r.schedule_id} dur={r.duration_minutes} "
                    f"floor={floor} full={full} {ctx}"
                )

        # 5. serialized: each adjustable run starts at/after previous run's end
        ordered = sorted(out, key=lambda r: r.start_at)
        for i in range(1, len(ordered)):
            cur = ordered[i]
            prev = ordered[i - 1]
            orig = sched_by_id[cur.schedule_id]
            if orig.reason == REASON_COMMITTED:
                continue  # committed runs are pinned, may overlap a prior committed
            prev_end = prev.start_at + timedelta(minutes=prev.duration_minutes)
            assert cur.start_at >= prev_end, (
                f"OVERLAP: {prev.schedule_id} end {prev_end} vs {cur.schedule_id} "
                f"start {cur.start_at} {ctx}"
            )
