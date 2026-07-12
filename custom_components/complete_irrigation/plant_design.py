"""Yard design report (v2) — ties plants + schedules + ETo together.

Pure logic, HA-free. Given the plants in the yard and the schedules that
water their zones, derive each loop's effective runtime + weekly frequency
from the schedules and run the hydraulics core to produce a per-loop design
report (per-plant emitter recommendations + OK/UNDER/OVER + conflict and
capacity warnings). This is the engine the panel will surface; the
coordinator feeds it the live PlantStore + ScheduleStore + an ETo value.

A zone (the physical drip "loop") may be watered by several schedules and a
schedule may water several zones. We report each zone against its PRIMARY
schedule — the one delivering the most weekly water to that zone — because
the hydraulics model is per-run (emitter GPH during a run), and note when
other schedules also touch the zone so the picture stays honest.
"""

from __future__ import annotations

from dataclasses import replace

from .hydraulics import (
    DEFAULT_EFFICIENCY,
    Loop,
    LoopReport,
    PlantWaterResult,
    build_loop_report,
)
from .plant import PlantRecord
from .schedule import MODE_INTERVAL, MODE_INTERVAL_HOURS, Schedule


def schedule_runs_per_week(sched: Schedule) -> float:
    """How many times per week this schedule fires (a float; every-other-day
    interval = 3.5/wk). interval_hours is approximated by firings/day x 7."""
    if sched.mode == MODE_INTERVAL:
        return 7.0 / max(1, sched.interval_days or 1)
    if sched.mode == MODE_INTERVAL_HOURS:
        step = max(1, sched.interval_hours or 1)
        if sched.interval_end_time is not None:
            start_min = sched.start_time.hour * 60 + sched.start_time.minute
            end_min = sched.interval_end_time.hour * 60 + sched.interval_end_time.minute
            per_day = max(1, (end_min - start_min) // (step * 60) + 1)
        else:
            per_day = max(1, 24 // step)
        return per_day * 7.0
    # weekdays mode
    return float(len(sched.weekdays))


def zone_run_minutes(sched: Schedule, zone_entity_id: str) -> int:
    """Minutes this schedule runs the given zone in a single firing (summed
    across the zone's steps in a multi-zone schedule; 0 if it doesn't touch
    the zone)."""
    return sum(
        st.duration_minutes for st in sched.all_steps() if st.zone_entity_id == zone_entity_id
    )


def watering_schedules_for_zone(
    schedules: list[Schedule], zone_entity_id: str
) -> list[tuple[Schedule, int, float]]:
    """(schedule, run_minutes, weekly_minutes) for every ENABLED schedule that
    actually delivers water to the zone, most weekly water first."""
    out: list[tuple[Schedule, int, float]] = []
    for s in schedules:
        if not s.enabled:
            continue
        run_min = zone_run_minutes(s, zone_entity_id)
        weekly = run_min * schedule_runs_per_week(s)
        if run_min <= 0 or weekly <= 0:
            continue
        out.append((s, run_min, weekly))
    out.sort(key=lambda t: t[2], reverse=True)
    return out


def build_yard_report(
    plants: list[PlantRecord],
    schedules: list[Schedule],
    eto_in_week: float,
    efficiency: float = DEFAULT_EFFICIENCY,
    zone_capacity_gph: dict[str, float] | None = None,
) -> list[LoopReport]:
    """One LoopReport per zone that has plants, in plant-insertion order."""
    by_zone: dict[str, list[PlantRecord]] = {}
    for p in plants:
        by_zone.setdefault(p.zone_entity_id, []).append(p)

    capacity = zone_capacity_gph or {}
    reports: list[LoopReport] = []
    for zone, recs in by_zone.items():
        calc_plants = [r.to_calc_plant() for r in recs]
        cap = capacity.get(zone)
        watering = watering_schedules_for_zone(schedules, zone)

        if not watering:
            # Plants attached to a zone nothing waters — the most important
            # thing to surface, not a pile of inf-GPH emitter math.
            loop = Loop(zone, 0, 1, cap)
            rep = build_loop_report(loop, calc_plants, eto_in_week, efficiency)
            rep = replace(
                rep,
                warnings=(
                    "No enabled schedule waters this zone — attach a schedule, "
                    "or move these plants to a watered zone.",
                ),
            )
            reports.append(rep)
            continue

        primary, run_min, _ = watering[0]
        loop = Loop(zone, run_min, schedule_runs_per_week(primary), cap)
        rep = build_loop_report(loop, calc_plants, eto_in_week, efficiency)
        # v1.40.7 — carry the schedules watering this loop (the design reflects
        # the primary = most-weekly-water one; the rest are shown, not warned).
        sched_list = tuple(
            {
                "name": s.name,
                "runtime_minutes": rm,
                "runs_per_week": round(schedule_runs_per_week(s), 1),
                "primary": i == 0,
            }
            for i, (s, rm, _w) in enumerate(watering)
        )
        rep = replace(rep, schedules=sched_list)
        reports.append(rep)
    return reports


# ── JSON serialization for the ws_api / panel ───────────────────────


def serialize_plant_result(r: PlantWaterResult) -> dict:
    """JSON-safe dict for one plant's result (inf required GPH -> None)."""
    req = r.required_gph
    return {
        "name": r.plant.name,
        "wucols_category": r.plant.wucols_category,
        "canopy_area_sqft": r.plant.canopy_area_sqft,
        "need_gal_week": round(r.need_gal_week, 1),
        "required_gph": None if req == float("inf") else round(req, 2),
        "emitters": [{"count": c, "gph": s} for c, s in r.emitters],
        "emitter_gph": round(r.emitter_gph, 2),
        "delivered_gal_week": round(r.delivered_gal_week, 1),
        "status": r.status,
        "pct_off": round(r.pct_off, 3),
        # v1.39 — actual delivery from the plant's INSTALLED drips (None = unknown).
        "installed_delivered_gal_week": (
            round(r.installed_delivered_gal_week, 1)
            if r.installed_delivered_gal_week is not None
            else None
        ),
        "installed_status": r.installed_status,
        "installed_pct_off": (
            round(r.installed_pct_off, 3) if r.installed_pct_off is not None else None
        ),
    }


def serialize_topup(t) -> dict:
    """JSON-safe dict for one top-up recommendation."""
    return {
        "plant_name": t.plant_name,
        "deficit_gal_week": t.deficit_gal_week,
        "extra_runs_per_week": t.extra_runs_per_week,
        "extra_minutes": t.extra_minutes,
        "delivered_gal_week_after": t.delivered_gal_week_after,
        "feasible": t.feasible,
        "overwater": [{"plant_name": n, "extra_frac": f} for n, f in t.overwater],
    }


def serialize_loop_report(rep: LoopReport) -> dict:
    """JSON-safe dict for a per-loop design report (for ws_api / the panel)."""
    return {
        "zone_entity_id": rep.loop.id,
        "runtime_minutes": rep.loop.runtime_minutes,
        "runs_per_week": rep.loop.runs_per_week,
        "max_flow_gph": rep.loop.max_flow_gph,
        "total_flow_gph": round(rep.total_flow_gph, 2),
        "warnings": list(rep.warnings),
        "schedules": [dict(s) for s in rep.schedules],
        "suggested_runtime_minutes": rep.suggested_runtime_minutes,
        "plants": [serialize_plant_result(p) for p in rep.plants],
        "topups": [serialize_topup(t) for t in rep.topups],
    }
