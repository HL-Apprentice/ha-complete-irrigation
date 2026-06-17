"""Drip hydraulics — plant water needs and emitter sizing (v2).

Pure logic, HA-free (like conflict_resolver / run_guard), so the model is
unit-testable and the coordinator/services layer can call into it without
any I/O. This is the "brain" of v2's plant-aware irrigation: it knows what
each plant needs and tells you how to deliver it on a shared loop.

Model — the standard WUCOLS x ETo landscape-irrigation formula:

    need_gal_per_week = ETo_in_per_week * plant_factor * canopy_area_sqft
                        * GAL_PER_SQFT_INCH / drip_efficiency

`plant_factor` comes from the plant's WUCOLS water-use category; `ETo`
(reference evapotranspiration) comes from the weather. Every plant on a
loop shares the loop's runtime, so the only per-plant knob is the EMITTER
set (count x GPH). We size emitters so delivered ~= need, and surface the
cases a single shared runtime can't honor (mismatched plants, line-flow
limits) instead of emitting a confident-but-wrong config.

Validated by dev/prototypes/water_calc_proto.py before being lifted here.
"""

from __future__ import annotations

from dataclasses import dataclass

# WUCOLS water-use category -> plant factor (the sanctioned data; do not
# invent per-species figures beyond these category coefficients).
WUCOLS_FACTORS: dict[str, float] = {
    "very_low": 0.1,
    "low": 0.3,
    "moderate": 0.5,
    "high": 0.8,
}

# Standard inline-drip emitter sizes (US, GPH).
EMITTER_SIZES: tuple[float, ...] = (0.5, 1.0, 2.0, 4.0)

GAL_PER_SQFT_INCH = 0.623  # gallons to cover 1 sq ft with 1 inch of water
DEFAULT_EFFICIENCY = 0.9  # drip irrigation efficiency
WITHIN_TOLERANCE = 0.10  # +/-10% delivered vs need still counts as "OK"
MAX_EMITTERS_PER_PLANT = 10

# Design-conflict thresholds (when a shared runtime can't honor a plant).
_SUB_EMITTER_FACTOR = 0.75  # need < 0.75 x smallest emitter -> unavoidably over
_EMITTER_MISS_FRAC = 0.25  # closest emitter set >25% off need -> flag
_MISMATCH_RATIO = 5  # thirstiest needs >=5x the least -> split the loop
# Runtime search bounds for suggest_runtime (minutes): start, stop, step.
_RUNTIME_SEARCH = (5, 305, 5)

STATUS_OK = "OK"
STATUS_UNDER = "UNDER"
STATUS_OVER = "OVER"


@dataclass(frozen=True)
class Plant:
    """A plant attached to an irrigation loop."""

    name: str
    wucols_category: str
    canopy_area_sqft: float
    loop_id: str

    def __post_init__(self) -> None:
        if self.wucols_category not in WUCOLS_FACTORS:
            raise ValueError(
                f"unknown WUCOLS category {self.wucols_category!r}; "
                f"expected one of {sorted(WUCOLS_FACTORS)}"
            )

    @property
    def plant_factor(self) -> float:
        return WUCOLS_FACTORS[self.wucols_category]


@dataclass(frozen=True)
class Loop:
    """An irrigation loop (maps to a schedule/zone in the real system)."""

    id: str
    runtime_minutes: int
    runs_per_week: int
    max_flow_gph: float | None = None  # line/valve capacity, if known


@dataclass(frozen=True)
class PlantWaterResult:
    plant: Plant
    need_gal_week: float
    required_gph: float
    emitters: tuple[tuple[int, float], ...]  # (count, size) largest-first
    emitter_gph: float
    delivered_gal_week: float
    status: str  # STATUS_OK / STATUS_UNDER / STATUS_OVER
    pct_off: float


@dataclass(frozen=True)
class LoopReport:
    loop: Loop
    plants: tuple[PlantWaterResult, ...]
    total_flow_gph: float
    warnings: tuple[str, ...]
    suggested_runtime_minutes: int


# ── core formulas ───────────────────────────────────────────────────


def weekly_need_gal(plant: Plant, eto_in_week: float, efficiency: float) -> float:
    """Gallons the plant needs per week (WUCOLS x ETo landscape formula)."""
    inches = eto_in_week * plant.plant_factor
    return inches * plant.canopy_area_sqft * GAL_PER_SQFT_INCH / efficiency


def required_total_gph(plant: Plant, loop: Loop, eto_in_week: float, efficiency: float) -> float:
    """Total emitter flow (GPH) the plant needs at the loop's shared runtime."""
    hours_per_run = loop.runtime_minutes / 60.0
    if hours_per_run <= 0 or loop.runs_per_week <= 0:
        return float("inf")
    need_per_run = weekly_need_gal(plant, eto_in_week, efficiency) / loop.runs_per_week
    return need_per_run / hours_per_run


def recommend_emitters(required_gph: float) -> tuple[tuple[tuple[int, float], ...], float]:
    """Closest emitter multiset (>=1 emitter, <= MAX_EMITTERS_PER_PLANT) to
    `required_gph`. Returns ((count, size) largest-first, total_gph).

    Bounded brute force over the standard sizes; ties break toward the
    fewest emitters (the simplest install)."""
    best_key: tuple[float, int] | None = None
    best_counts: dict[float, int] = {}
    best_total = 0.0
    for n4 in range(4):
        for n2 in range(7):
            for n1 in range(9):
                for nh in range(9):
                    count = n4 + n2 + n1 + nh
                    if count < 1 or count > MAX_EMITTERS_PER_PLANT:
                        continue
                    total = 4 * n4 + 2 * n2 + 1 * n1 + 0.5 * nh
                    key = (round(abs(total - required_gph), 3), count)
                    if best_key is None or key < best_key:
                        best_key = key
                        best_counts = {4.0: n4, 2.0: n2, 1.0: n1, 0.5: nh}
                        best_total = total
    combo = tuple((c, s) for s, c in sorted(best_counts.items(), reverse=True) if c > 0)
    return combo, best_total


def delivered_gal_week(total_gph: float, loop: Loop) -> float:
    return total_gph * (loop.runtime_minutes / 60.0) * loop.runs_per_week


def classify(delivered: float, need: float, tol: float = WITHIN_TOLERANCE) -> tuple[str, float]:
    """Return (status, fractional_offset) for delivered vs need."""
    if need <= 0:
        return (STATUS_OK, 0.0)
    pct = (delivered - need) / need
    if abs(pct) <= tol:
        return (STATUS_OK, pct)
    return (STATUS_OVER if pct > 0 else STATUS_UNDER, pct)


# ── per-plant + per-loop reports ────────────────────────────────────


def evaluate_plant(plant: Plant, loop: Loop, eto: float, efficiency: float) -> PlantWaterResult:
    need = weekly_need_gal(plant, eto, efficiency)
    req_gph = required_total_gph(plant, loop, eto, efficiency)
    combo, total_gph = recommend_emitters(req_gph)
    delivered = delivered_gal_week(total_gph, loop)
    status, pct = classify(delivered, need)
    return PlantWaterResult(
        plant=plant,
        need_gal_week=need,
        required_gph=req_gph,
        emitters=combo,
        emitter_gph=total_gph,
        delivered_gal_week=delivered,
        status=status,
        pct_off=pct,
    )


def find_design_conflicts(results: list[PlantWaterResult]) -> list[str]:
    """Surface plants a single shared runtime can't honor with sane emitters."""
    out: list[str] = []
    finite = [r.required_gph for r in results if r.required_gph not in (0, float("inf"))]
    for r in results:
        req = r.required_gph
        if req == float("inf"):
            continue
        if req < EMITTER_SIZES[0] * _SUB_EMITTER_FACTOR:
            out.append(
                f"{r.plant.name}: needs only {req:.2f} GPH — less than one "
                f"{EMITTER_SIZES[0]} GPH emitter delivers. Shorten this loop's runtime "
                f"or move it to its own loop."
            )
        elif r.emitter_gph and abs(r.emitter_gph - req) / max(req, 0.1) > _EMITTER_MISS_FRAC:
            out.append(
                f"{r.plant.name}: closest emitter set ({r.emitter_gph:.1f} GPH) is "
                f">{_EMITTER_MISS_FRAC:.0%} off its {req:.2f} GPH need at this runtime."
            )
    if len(finite) >= 2 and min(finite) > 0 and max(finite) / min(finite) >= _MISMATCH_RATIO:
        out.append(
            f"Mismatched plants share this loop (thirstiest needs "
            f"{max(finite) / min(finite):.0f}x the least). Consider splitting them onto "
            f"separate loops — emitter tuning alone can't fully reconcile them."
        )
    return out


def suggest_runtime(loop: Loop, plants: list[Plant], eto: float, efficiency: float) -> int:
    """Runtime (min) within the search bounds that minimizes total emitter-
    quantization error across the loop's plants — i.e. where everyone's need
    lands nearest a clean emitter set. Falls back to the current runtime."""
    best_min, best_err = loop.runtime_minutes, float("inf")
    for t in range(*_RUNTIME_SEARCH):
        probe = Loop(loop.id, t, loop.runs_per_week, loop.max_flow_gph)
        err = 0.0
        for p in plants:
            req = required_total_gph(p, probe, eto, efficiency)
            _, total = recommend_emitters(req)
            err += abs(total - req)
        if err < best_err - 1e-9:
            best_err, best_min = err, t
    return best_min


def build_loop_report(loop: Loop, plants: list[Plant], eto: float, efficiency: float) -> LoopReport:
    results = [evaluate_plant(p, loop, eto, efficiency) for p in plants]
    total_flow = sum(r.emitter_gph for r in results)
    warnings: list[str] = []
    if loop.max_flow_gph is not None and total_flow > loop.max_flow_gph:
        warnings.append(
            f"Loop flow {total_flow:.1f} GPH exceeds capacity {loop.max_flow_gph:.1f} GPH "
            f"— plants will under-deliver. Reduce emitters, run longer/more often, "
            f"or split the loop."
        )
    warnings.extend(find_design_conflicts(results))
    return LoopReport(
        loop=loop,
        plants=tuple(results),
        total_flow_gph=total_flow,
        warnings=tuple(warnings),
        suggested_runtime_minutes=suggest_runtime(loop, plants, eto, efficiency),
    )
