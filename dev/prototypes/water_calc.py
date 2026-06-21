"""PROTOTYPE — drip water calculator (the keeper bit).

QUESTION THIS ANSWERS: can plants with different water needs share ONE
irrigation loop and each still get the right amount of water by tuning
EMITTERS (count x GPH), given that every plant on a loop shares the same
runtime? And does the WUCOLS-plant-factor x ETo model produce sane,
fine-tunable emitter recommendations?

This module is PURE (no I/O, no terminal code). The throwaway TUI in
water_calc_proto.py imports it. If the model proves out, this lifts onto
the v2 branch of ha-complete-irrigation; the TUI gets deleted.

Model (standard WUCOLS + ETo landscape formula):
    weekly_need_gal = ETo_in_per_week * plant_factor * canopy_area_sqft
                      * 0.623 / drip_efficiency
Then size emitters so delivered ~= need at the loop's shared runtime:
    required_total_gph = (weekly_need / runs_per_week) / (runtime_min / 60)
"""

from __future__ import annotations

from dataclasses import dataclass

# WUCOLS water-use category -> plant factor (the sanctioned data).
WUCOLS_FACTORS = {"very_low": 0.1, "low": 0.3, "moderate": 0.5, "high": 0.8}

# Standard inline-drip emitter sizes (US, GPH).
EMITTER_SIZES = (0.5, 1.0, 2.0, 4.0)

GAL_PER_SQFT_INCH = 0.623  # gallons to cover 1 sq ft with 1 inch of water
DEFAULT_EFFICIENCY = 0.9  # drip irrigation efficiency
WITHIN_TOLERANCE = 0.10  # +/-10% delivered vs need = "OK"
MAX_EMITTERS_PER_PLANT = 10


@dataclass(frozen=True)
class Plant:
    name: str
    wucols_category: str  # one of WUCOLS_FACTORS
    canopy_area_sqft: float
    loop_id: str

    @property
    def plant_factor(self) -> float:
        return WUCOLS_FACTORS[self.wucols_category]


@dataclass(frozen=True)
class Loop:
    id: str
    runtime_minutes: int
    runs_per_week: int
    max_flow_gph: float | None = None  # line/valve capacity, if known


# ── core formulas ───────────────────────────────────────────────────


def weekly_need_gal(plant: Plant, eto_in_week: float, efficiency: float) -> float:
    """Gallons the plant needs per week (WUCOLS x ETo landscape formula)."""
    inches = eto_in_week * plant.plant_factor
    return inches * plant.canopy_area_sqft * GAL_PER_SQFT_INCH / efficiency


def required_total_gph(plant: Plant, loop: Loop, eto_in_week: float, efficiency: float) -> float:
    """Total emitter flow (GPH) the plant needs, given the loop's shared
    runtime and weekly run count."""
    need_week = weekly_need_gal(plant, eto_in_week, efficiency)
    need_per_run = need_week / loop.runs_per_week
    hours_per_run = loop.runtime_minutes / 60.0
    if hours_per_run <= 0:
        return float("inf")
    return need_per_run / hours_per_run


def recommend_emitters(required_gph: float) -> tuple[list[tuple[int, float]], float]:
    """Closest emitter multiset (>=1 emitter, <= MAX_EMITTERS_PER_PLANT) to
    `required_gph`. Returns ([(count, size), ...] largest-first, total_gph).

    Bounded brute force over the 4 standard sizes; ties break toward the
    fewest emitters (simplest install)."""
    best_key: tuple[float, int] | None = None
    best: dict[float, int] = {}
    best_total = 0.0
    for n4 in range(0, 4):
        for n2 in range(0, 7):
            for n1 in range(0, 9):
                for nh in range(0, 9):
                    count = n4 + n2 + n1 + nh
                    if count < 1 or count > MAX_EMITTERS_PER_PLANT:
                        continue
                    total = 4 * n4 + 2 * n2 + 1 * n1 + 0.5 * nh
                    key = (round(abs(total - required_gph), 3), count)
                    if best_key is None or key < best_key:
                        best_key = key
                        best = {4.0: n4, 2.0: n2, 1.0: n1, 0.5: nh}
                        best_total = total
    combo = [(c, s) for s, c in sorted(best.items(), reverse=True) if c > 0]
    return combo, best_total


def delivered_gal_week(total_gph: float, loop: Loop) -> float:
    return total_gph * (loop.runtime_minutes / 60.0) * loop.runs_per_week


def classify(delivered: float, need: float, tol: float = WITHIN_TOLERANCE) -> tuple[str, float]:
    """Return ("OK"|"UNDER"|"OVER", pct_off) for delivered vs need."""
    if need <= 0:
        return ("OK", 0.0)
    pct = (delivered - need) / need
    if abs(pct) <= tol:
        return ("OK", pct)
    return ("OVER" if pct > 0 else "UNDER", pct)


# ── per-plant + per-loop reports ────────────────────────────────────


def plant_row(plant: Plant, loop: Loop, eto: float, efficiency: float) -> dict:
    need = weekly_need_gal(plant, eto, efficiency)
    req_gph = required_total_gph(plant, loop, eto, efficiency)
    combo, total_gph = recommend_emitters(req_gph)
    delivered = delivered_gal_week(total_gph, loop)
    status, pct = classify(delivered, need)
    return {
        "plant": plant,
        "need_week": need,
        "required_gph": req_gph,
        "emitters": combo,
        "emitter_gph": total_gph,
        "delivered_week": delivered,
        "status": status,
        "pct_off": pct,
    }


def design_conflicts(rows: list[dict]) -> list[str]:
    """Surface plants a single shared runtime can't honor with sane emitters."""
    out: list[str] = []
    reqs = [r["required_gph"] for r in rows if r["required_gph"] not in (0, float("inf"))]
    for r in rows:
        req = r["required_gph"]
        if req == float("inf"):
            continue
        if req < EMITTER_SIZES[0] * 0.75:  # below ~0.4 GPH: even one 0.5 over-waters
            out.append(
                f"{r['plant'].name}: needs only {req:.2f} GPH — less than one "
                f"0.5 GPH emitter delivers. Shorten this loop's runtime or move it off."
            )
        if r["emitter_gph"] and abs(r["emitter_gph"] - req) / max(req, 0.1) > 0.25:
            out.append(
                f"{r['plant'].name}: closest emitter set ({r['emitter_gph']:.1f} GPH) "
                f"is >25% off its {req:.2f} GPH need at this runtime."
            )
    if len(reqs) >= 2 and min(reqs) > 0 and max(reqs) / min(reqs) >= 5:
        out.append(
            f"Mismatched plants share this loop (thirstiest needs "
            f"{max(reqs) / min(reqs):.0f}x the least). Consider splitting them onto "
            f"separate loops — emitter tuning alone can't fully reconcile them."
        )
    return out


def suggest_runtime(loop: Loop, plants: list[Plant], eto: float, efficiency: float) -> int:
    """Runtime (min) in 5..300 that minimizes total emitter-quantization error
    across the loop's plants — i.e. where everyone's need lands nearest a clean
    emitter set. Demonstrates 'the app suggests a runtime'."""
    best_min, best_err = loop.runtime_minutes, float("inf")
    for t in range(5, 305, 5):
        probe = Loop(loop.id, t, loop.runs_per_week, loop.max_flow_gph)
        err = 0.0
        for p in plants:
            req = required_total_gph(p, probe, eto, efficiency)
            _, total = recommend_emitters(req)
            err += abs(total - req)
        if err < best_err - 1e-9:
            best_err, best_min = err, t
    return best_min


def loop_report(loop: Loop, plants: list[Plant], eto: float, efficiency: float) -> dict:
    rows = [plant_row(p, loop, eto, efficiency) for p in plants]
    total_flow = sum(r["emitter_gph"] for r in rows)
    warnings: list[str] = []
    if loop.max_flow_gph is not None and total_flow > loop.max_flow_gph:
        warnings.append(
            f"Loop flow {total_flow:.1f} GPH exceeds capacity {loop.max_flow_gph:.1f} GPH "
            f"— plants will under-deliver. Reduce emitters or split the loop."
        )
    warnings.extend(design_conflicts(rows))
    return {
        "loop": loop,
        "rows": rows,
        "total_flow_gph": total_flow,
        "warnings": warnings,
        "suggested_runtime": suggest_runtime(loop, plants, eto, efficiency),
    }
