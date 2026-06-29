"""Tests for the daily planner (pure logic, v1.32).

The deterministic advisory plan + the LLM-ordering rail. Covers urgency blending,
per-band recommendations, prioritized ordering, the human summary, and the safety
bounds that gate a (quarantined) LLM's proposed re-ordering.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from custom_components.complete_irrigation.daily_planner import (
    BAND_LIGHT,
    BAND_SATURATED,
    BAND_URGENT,
    REC_LIGHT,
    REC_PRIORITY,
    REC_RUN,
    REC_SKIP,
    DailyPlan,
    ZoneSignal,
    build_daily_plan,
    reconcile_llm_ordering,
    serialize_daily_plan,
    zone_urgency,
)

T = datetime(2026, 5, 18, 6, 0)


@dataclass
class _Run:
    zone_entity_id: str
    start_at: datetime
    duration_minutes: int = 20
    schedule_id: str = "s"
    schedule_name: str = "S"


def _run(zone, offset_min=0, **kw):
    return _Run(zone_entity_id=zone, start_at=T + timedelta(minutes=offset_min), **kw)


# ── zone_urgency ────────────────────────────────────────────────────


def test_saturated_zone_has_zero_urgency():
    assert zone_urgency(ZoneSignal(deficit_frac=1.0, band=BAND_SATURATED, eto_factor=1.0)) == 0.0


def test_below_min_moisture_forces_high_urgency():
    # Even with no modeled deficit, below-min moisture is urgent on its own.
    assert zone_urgency(ZoneSignal(deficit_frac=0.0, band=BAND_URGENT)) >= 0.85


def test_deficit_and_eto_blend():
    u = zone_urgency(ZoneSignal(deficit_frac=1.0, band="", eto_factor=1.0))
    assert u == 1.0
    u2 = zone_urgency(ZoneSignal(deficit_frac=0.0, band="", eto_factor=0.0))
    assert u2 == 0.0


def test_light_band_softens_urgency():
    full = zone_urgency(ZoneSignal(deficit_frac=1.0, band="", eto_factor=1.0))
    light = zone_urgency(ZoneSignal(deficit_frac=1.0, band=BAND_LIGHT, eto_factor=1.0))
    assert light < full


def test_urgency_is_clamped():
    assert zone_urgency(ZoneSignal(deficit_frac=5.0, band="", eto_factor=5.0)) == 1.0
    assert zone_urgency(ZoneSignal(deficit_frac=-3.0, band="", eto_factor=-3.0)) == 0.0


# ── build_daily_plan: recommendations + ordering ────────────────────


def test_plan_orders_priority_first_skip_last():
    runs = [
        _run("switch.wet", 0),  # saturated -> skip
        _run("switch.dry", 10),  # high deficit -> priority
        _run("switch.ok", 20),  # normal -> run
        _run("switch.near", 30),  # light
    ]
    signals = {
        "switch.wet": ZoneSignal(band=BAND_SATURATED),
        "switch.dry": ZoneSignal(deficit_frac=1.0, eto_factor=1.0),
        "switch.ok": ZoneSignal(deficit_frac=0.2, eto_factor=0.2),
        "switch.near": ZoneSignal(deficit_frac=0.3, band=BAND_LIGHT),
    }
    plan = build_daily_plan(runs, signals=signals)
    order = [it.zone_entity_id for it in plan.items]
    assert order[0] == "switch.dry"  # priority first
    assert order[-1] == "switch.wet"  # skip last
    recs = {it.zone_entity_id: it.recommendation for it in plan.items}
    assert recs["switch.dry"] == REC_PRIORITY
    assert recs["switch.wet"] == REC_SKIP
    assert recs["switch.ok"] == REC_RUN
    assert recs["switch.near"] == REC_LIGHT


def test_zone_without_signal_defaults_to_run():
    plan = build_daily_plan([_run("switch.x")], signals={})
    assert plan.items[0].recommendation == REC_RUN


def test_zone_names_applied():
    plan = build_daily_plan(
        [_run("switch.citrus")], signals={}, zone_names={"switch.citrus": "Citrus"}
    )
    assert plan.items[0].zone_name == "Citrus"


def test_empty_plan_summary():
    plan = build_daily_plan([], signals={})
    assert plan.items == ()
    assert "No runs" in plan.summary


def test_summary_mentions_priority_zone():
    runs = [_run("switch.dry", 0)]
    signals = {"switch.dry": ZoneSignal(deficit_frac=1.0, eto_factor=1.0)}
    plan = build_daily_plan(runs, signals=signals, zone_names={"switch.dry": "Back Beds"})
    assert "priority" in plan.summary
    assert "Back Beds" in plan.summary


# ── reconcile_llm_ordering: the safety rail ─────────────────────────


def _two_zone_plan():
    runs = [_run("switch.a", 0), _run("switch.b", 10)]
    signals = {
        "switch.a": ZoneSignal(deficit_frac=0.2),
        "switch.b": ZoneSignal(deficit_frac=0.9, eto_factor=0.9),
    }
    return build_daily_plan(runs, signals=signals)


def test_llm_valid_reorder_is_applied():
    plan = _two_zone_plan()  # rail order puts b (priority) first
    # LLM proposes a different but valid permutation.
    reordered = reconcile_llm_ordering(plan, ["switch.a", "switch.b"])
    assert [it.zone_entity_id for it in reordered.items] == ["switch.a", "switch.b"]


def test_llm_wrong_set_falls_back_to_rail():
    plan = _two_zone_plan()
    rail_order = [it.zone_entity_id for it in plan.items]

    def _order(llm):
        return [i.zone_entity_id for i in reconcile_llm_ordering(plan, llm).items]

    # Extra zone / missing zone / dupes -> reject, keep the rail order.
    assert _order(["switch.a"]) == rail_order
    assert _order(["switch.a", "switch.c"]) == rail_order
    assert _order(["switch.a", "switch.a", "switch.b"]) == rail_order


def test_llm_cannot_float_a_skip_ahead_of_real_work():
    runs = [_run("switch.wet", 0), _run("switch.dry", 10)]
    signals = {
        "switch.wet": ZoneSignal(band=BAND_SATURATED),  # skip
        "switch.dry": ZoneSignal(deficit_frac=1.0, eto_factor=1.0),  # priority
    }
    plan = build_daily_plan(runs, signals=signals)
    rail_order = [it.zone_entity_id for it in plan.items]  # dry, then wet(skip)
    # LLM tries to put the saturated (skip) zone first -> rejected, rail kept.
    out = reconcile_llm_ordering(plan, ["switch.wet", "switch.dry"])
    assert [it.zone_entity_id for it in out.items] == rail_order


def test_reconcile_returns_dailyplan_type():
    plan = _two_zone_plan()
    assert isinstance(reconcile_llm_ordering(plan, ["switch.b", "switch.a"]), DailyPlan)


# ── serialization ───────────────────────────────────────────────────


def test_serialize_daily_plan_is_json_safe():
    runs = [_run("switch.dry", 0, schedule_id="d", schedule_name="Drip")]
    signals = {"switch.dry": ZoneSignal(deficit_frac=1.0, eto_factor=1.0)}
    plan = build_daily_plan(runs, signals=signals, zone_names={"switch.dry": "Beds"})
    out = serialize_daily_plan(plan)
    assert out["summary"] == plan.summary
    assert len(out["items"]) == 1
    it = out["items"][0]
    assert it["zone_entity_id"] == "switch.dry"
    assert it["zone_name"] == "Beds"
    assert it["recommendation"] == REC_PRIORITY
    assert isinstance(it["start_at"], str)  # ISO string, not a datetime
    assert it["duration_minutes"] == 20
    import json

    json.dumps(out)  # must not raise
