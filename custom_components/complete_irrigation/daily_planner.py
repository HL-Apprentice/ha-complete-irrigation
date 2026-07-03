"""Daily plan — a pure-logic advisory briefing for the day's watering (v1.32).

Given the day's planned runs plus per-zone urgency signals (weekly water deficit,
moisture band, ET demand), this produces a PRIORITIZED, annotated plan: which zones
are most urgent (driest / hottest), which can be skipped (saturated), and in what
order to water for plant health.

ADVISORY ONLY — it never changes what actually fires; the scheduler + moisture /
weather gates + the one-zone resolver still own actuation. The plan is surfaced to
the user (and to the LLM advisor) as guidance.

It is also the deterministic RAIL the (quarantined, low-precision) LLM advisor is
validated against. `reconcile_llm_ordering` lets the LLM RE-ORDER the day's runs by
its own reasoning, but only within safe bounds: it must propose the SAME set of
zones, and it can never un-skip a zone the rail marked SKIP (saturated) nor invent a
run. Anything off — extra/missing zones, a saturated zone promoted to run — and we
fall back to the deterministic order. The model can make the plan smarter; it can
never make it unsafe.

Pure + HA-free (like hydraulics / et_calc / conflict_resolver), so it is fully
unit-testable without standing up Home Assistant.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# Recommendation tags, most-urgent first. Ordering rank is their index here.
REC_PRIORITY = "priority"  # very dry / below-min moisture -> water first
REC_RUN = "run"  # normal
REC_LIGHT = "light"  # near target -> a light run is enough
REC_SKIP = "skip"  # saturated -> recommend skipping today
_REC_RANK = {REC_PRIORITY: 0, REC_RUN: 1, REC_LIGHT: 2, REC_SKIP: 3}

# Moisture bands (mirrors moisture_gate, kept as plain strings to stay HA-free).
BAND_URGENT = "urgent"  # below min -> always priority
BAND_NORMAL = "normal"
BAND_LIGHT = "light"  # near target
BAND_SATURATED = "saturated"  # >= max -> skip

_PRIORITY_THRESHOLD = 0.66  # urgency at/above this (and not saturated) -> priority


def _clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else 1.0 if v > 1.0 else v


@dataclass(frozen=True)
class ZoneSignal:
    """Per-zone urgency inputs (all pre-normalized, HA-free).

    deficit_frac : weekly water shortfall as a fraction of weekly need (0 = met,
                   1 = getting nothing) — from the hydraulics yard report.
    band         : current moisture band (BAND_* / "" when no sensor).
    eto_factor   : normalized ET demand for the week (0 = none, 1 = peak) — the
                   climate "how thirsty is it" term, same for every zone.
    """

    deficit_frac: float = 0.0
    band: str = ""
    eto_factor: float = 0.0


@dataclass(frozen=True)
class DayItem:
    zone_entity_id: str
    zone_name: str
    schedule_id: str
    schedule_name: str
    start_at: datetime
    duration_minutes: int
    recommendation: str
    urgency: float  # 0..1
    reason: str


@dataclass(frozen=True)
class DailyPlan:
    items: tuple[DayItem, ...]  # ordered: recommendation rank, then urgency desc, then time
    summary: str


def zone_urgency(sig: ZoneSignal) -> float:
    """Blend the per-zone signals into a 0..1 urgency. Saturated forces 0 (don't
    water); below-min moisture forces high (water regardless of the weekly model)."""
    if sig.band == BAND_SATURATED:
        return 0.0
    base = _clamp01(0.6 * _clamp01(sig.deficit_frac) + 0.4 * _clamp01(sig.eto_factor))
    if sig.band == BAND_URGENT:
        return max(base, 0.85)  # below min moisture is urgent on its own
    if sig.band == BAND_LIGHT:
        return base * 0.5  # near target — soften
    return base


def recommend(sig: ZoneSignal, urgency: float) -> tuple[str, str]:
    """(recommendation, human reason) for a zone from its band + blended urgency."""
    if sig.band == BAND_SATURATED:
        return REC_SKIP, "Soil is saturated (moisture at/above max) — recommend skipping today."
    if sig.band == BAND_URGENT:
        return REC_PRIORITY, "Moisture is below minimum — water this zone first."
    if urgency >= _PRIORITY_THRESHOLD:
        return REC_PRIORITY, "High water deficit and ET demand — water this zone early."
    if sig.band == BAND_LIGHT:
        return REC_LIGHT, "Moisture is near target — a light run is enough."
    return REC_RUN, "On track — run as scheduled."


def build_daily_plan(
    planned_runs,
    *,
    signals: dict[str, ZoneSignal],
    zone_names: dict[str, str] | None = None,
    now: datetime | None = None,
) -> DailyPlan:
    """Build the prioritized advisory plan for the day from the resolved runs and
    per-zone signals. `planned_runs` is an iterable of objects with
    zone_entity_id / start_at / duration_minutes / schedule_id / schedule_name
    (a PlannedRun, but kept duck-typed so this stays HA-free). A zone with no
    signal is treated as a plain RUN (no sensor / no deficit data = run as planned).
    """
    names = zone_names or {}
    items: list[DayItem] = []
    for r in planned_runs:
        zone = r.zone_entity_id
        sig = signals.get(zone, ZoneSignal())
        urg = zone_urgency(sig)
        rec, reason = recommend(sig, urg)
        items.append(
            DayItem(
                zone_entity_id=zone,
                zone_name=names.get(zone, zone),
                schedule_id=getattr(r, "schedule_id", "") or "",
                schedule_name=getattr(r, "schedule_name", "") or "",
                start_at=r.start_at,
                duration_minutes=int(r.duration_minutes),
                recommendation=rec,
                urgency=round(urg, 3),
                reason=reason,
            )
        )
    items.sort(key=lambda it: (_REC_RANK.get(it.recommendation, 1), -it.urgency, it.start_at))
    return DailyPlan(items=tuple(items), summary=_summarize(items))


def _summarize(items: list[DayItem]) -> str:
    if not items:
        return "No runs planned today."
    n = len(items)
    counts = {REC_PRIORITY: 0, REC_RUN: 0, REC_LIGHT: 0, REC_SKIP: 0}
    for it in items:
        counts[it.recommendation] = counts.get(it.recommendation, 0) + 1
    parts = [f"{n} run{'s' if n != 1 else ''} planned"]
    if counts[REC_PRIORITY]:
        top = next(it for it in items if it.recommendation == REC_PRIORITY)
        parts.append(f"{counts[REC_PRIORITY]} priority (water {top.zone_name} first)")
    if counts[REC_LIGHT]:
        parts.append(f"{counts[REC_LIGHT]} light")
    if counts[REC_SKIP]:
        parts.append(f"{counts[REC_SKIP]} to skip (saturated)")
    return "Today: " + ", ".join(parts) + "."


def serialize_daily_plan(plan: DailyPlan) -> dict:
    """JSON-safe dict for the daily plan (health.json + ws_api / panel)."""
    return {
        "summary": plan.summary,
        "items": [
            {
                "zone_entity_id": it.zone_entity_id,
                "zone_name": it.zone_name,
                "schedule_id": it.schedule_id,
                "schedule_name": it.schedule_name,
                "start_at": it.start_at.isoformat(),
                "duration_minutes": it.duration_minutes,
                "recommendation": it.recommendation,
                "urgency": it.urgency,
                "reason": it.reason,
            }
            for it in plan.items
        ],
    }


def reconcile_llm_ordering(plan: DailyPlan, llm_order: list[str]) -> DailyPlan:
    """Apply an LLM-proposed ORDER (list of zone_entity_ids) to the deterministic
    plan — but only if it is SAFE. The LLM may re-sequence the day's runs by its own
    reasoning; it may NOT change WHAT runs or override a gate. We accept its order
    only when it is a permutation of exactly the same zones AND it does not move a
    SKIP item ahead of any actionable (priority/run/light) item — i.e. it can't
    smuggle a saturated zone back into watering. Otherwise we keep the rail's order.

    Returns a DailyPlan with items in the accepted order (summary unchanged).
    """
    by_zone = {it.zone_entity_id: it for it in plan.items}
    # Must be a permutation of exactly the planned zones (no adds, drops, dupes).
    if sorted(llm_order) != sorted(by_zone):
        return plan
    reordered = [by_zone[z] for z in llm_order]
    # A SKIP item must never sit before an actionable one in the LLM order (that
    # would present a saturated zone as something to do before real work).
    seen_actionable_after_skip = False
    skipped_yet = False
    for it in reordered:
        if it.recommendation == REC_SKIP:
            skipped_yet = True
        elif skipped_yet:
            seen_actionable_after_skip = True
            break
    if seen_actionable_after_skip:
        return plan
    return DailyPlan(items=tuple(reordered), summary=plan.summary)
