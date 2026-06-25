"""Tests for the drip hydraulics core (pure logic, v2).

Covers the WUCOLS x ETo need formula, emitter sizing, delivered-vs-need
classification, design-conflict detection, and the per-loop report — the
"brain" of v2's plant-aware irrigation, validated before any UI is wired.
"""

from __future__ import annotations

import math

import pytest

from custom_components.complete_irrigation.hydraulics import (
    DEFAULT_EFFICIENCY,
    MAX_EMITTERS_PER_PLANT,
    STATUS_OK,
    STATUS_OVER,
    STATUS_UNDER,
    TOPUP_MAX_MINUTES,
    Loop,
    Plant,
    PlantWaterResult,
    build_loop_report,
    classify,
    delivered_gal_week,
    evaluate_plant,
    find_design_conflicts,
    recommend_emitters,
    recommend_topups,
    required_total_gph,
    suggest_runtime,
    weekly_need_gal,
)


def _plant(cat="moderate", area=100.0, loop="L"):
    return Plant(name=f"{cat}-plant", wucols_category=cat, canopy_area_sqft=area, loop_id=loop)


def _result(name, need, delivered, emitter_gph, status):
    """Hand-built PlantWaterResult so top-up logic is tested in isolation."""
    return PlantWaterResult(
        plant=Plant(name=name, wucols_category="moderate", canopy_area_sqft=100.0, loop_id="L"),
        need_gal_week=need,
        required_gph=1.0,
        emitters=((1, 1.0),),
        emitter_gph=emitter_gph,
        delivered_gal_week=delivered,
        status=status,
        pct_off=((delivered - need) / need) if need else 0.0,
    )


# ── WUCOLS x ETo need formula ───────────────────────────────────────


def test_weekly_need_matches_formula():
    # 2.0 in/wk x 0.8 (high) x 100 sqft x 0.623 / 0.9
    p = _plant("high", 100.0)
    expected = 2.0 * 0.8 * 100.0 * 0.623 / 0.9
    assert weekly_need_gal(p, 2.0, 0.9) == pytest.approx(expected)


def test_plant_factor_from_category():
    assert _plant("very_low").plant_factor == 0.1
    assert _plant("high").plant_factor == 0.8


def test_unknown_category_rejected():
    with pytest.raises(ValueError):
        Plant("x", "ultra", 10, "L")


def test_higher_category_needs_more_water():
    eto, eff = 2.0, DEFAULT_EFFICIENCY
    low = weekly_need_gal(_plant("low", 100), eto, eff)
    high = weekly_need_gal(_plant("high", 100), eto, eff)
    assert high > low


# ── required GPH at a shared runtime ────────────────────────────────


def test_required_gph_inverse_to_runtime():
    p = _plant("moderate", 100)
    short = required_total_gph(p, Loop("L", 60, 2), 2.0, 0.9)
    long = required_total_gph(p, Loop("L", 240, 2), 2.0, 0.9)
    # 4x the runtime -> ~1/4 the required flow
    assert long == pytest.approx(short / 4, rel=1e-6)


def test_zero_runtime_is_infinite_requirement():
    assert required_total_gph(_plant(), Loop("L", 0, 2), 2.0, 0.9) == float("inf")


# ── emitter recommendation ──────────────────────────────────────────


def test_exact_single_size_match():
    combo, total = recommend_emitters(2.0)
    assert total == 2.0
    assert combo == ((1, 2.0),)


def test_prefers_fewest_emitters_on_tie():
    # 3.5 GPH: 1x2 + 1x1 + 1x0.5 (3 emitters) beats 7x0.5 (7 emitters).
    combo, total = recommend_emitters(3.5)
    assert total == pytest.approx(3.5)
    assert sum(c for c, _ in combo) == 3


def test_sub_minimum_need_floors_at_one_small_emitter():
    combo, total = recommend_emitters(0.22)
    assert total == 0.5  # can't deliver less than one 0.5 GPH emitter
    assert combo == ((1, 0.5),)


def test_never_exceeds_emitter_cap():
    combo, _ = recommend_emitters(1000.0)
    assert 1 <= sum(c for c, _ in combo) <= MAX_EMITTERS_PER_PLANT


# ── delivered + classification ──────────────────────────────────────


def test_delivered_scales_with_runtime_and_runs():
    loop = Loop("L", 60, 3)  # 1 hour, 3x/week
    assert delivered_gal_week(2.0, loop) == pytest.approx(2.0 * 1.0 * 3)


def test_classify_bands():
    assert classify(100, 100)[0] == STATUS_OK
    assert classify(105, 100)[0] == STATUS_OK  # within 10%
    assert classify(80, 100)[0] == STATUS_UNDER
    assert classify(130, 100)[0] == STATUS_OVER
    assert classify(50, 0)[0] == STATUS_OK  # no need -> never flagged


# ── design conflicts ────────────────────────────────────────────────


def test_cactus_too_thirsty_neighbor_flagged():
    # very-low cactus + high citrus on one loop: the cactus over-waters and
    # the ratio trips the "split the loop" warning.
    loop = Loop("shrubs", 135, 3)
    cactus = _plant("very_low", 9, "shrubs")
    citrus = _plant("high", 100, "shrubs")
    results = [evaluate_plant(cactus, loop, 1.8, 0.9), evaluate_plant(citrus, loop, 1.8, 0.9)]
    conflicts = find_design_conflicts(results)
    assert any("split" in c.lower() for c in conflicts)
    assert any(cactus.name in c for c in conflicts)


def test_matched_plants_have_no_conflicts():
    loop = Loop("citrus", 240, 2)
    results = [
        evaluate_plant(_plant("high", 110, "citrus"), loop, 1.8, 0.9),
        evaluate_plant(_plant("high", 95, "citrus"), loop, 1.8, 0.9),
    ]
    assert find_design_conflicts(results) == []


# ── loop report ─────────────────────────────────────────────────────


def test_loop_report_flags_over_capacity():
    # Two thirsty citrus on a low-capacity line -> capacity warning.
    loop = Loop("citrus", 240, 2, max_flow_gph=4)
    plants = [_plant("high", 113, "citrus"), _plant("high", 95, "citrus")]
    rep = build_loop_report(loop, plants, 2.0, 0.9)
    assert rep.total_flow_gph > 4
    assert any("capacity" in w.lower() for w in rep.warnings)
    assert len(rep.plants) == 2


def test_suggested_runtime_in_bounds_and_multiple_of_step():
    loop = Loop("L", 135, 3)
    plants = [_plant("low", 16, "L"), _plant("moderate", 30, "L")]
    t = suggest_runtime(loop, plants, 1.8, 0.9)
    assert 5 <= t <= 300
    assert t % 5 == 0


def test_suggested_runtime_reduces_quantization_error():
    # The suggested runtime should be no worse than the loop's current one.
    loop = Loop("L", 137, 3)  # deliberately awkward current runtime
    plants = [_plant("moderate", 30, "L")]

    def total_err(t):
        probe = Loop("L", t, 3)
        return sum(
            abs(
                recommend_emitters(required_total_gph(p, probe, 1.8, 0.9))[1]
                - required_total_gph(p, probe, 1.8, 0.9)
            )
            for p in plants
        )

    suggested = suggest_runtime(loop, plants, 1.8, 0.9)
    assert total_err(suggested) <= total_err(loop.runtime_minutes) + 1e-9
    assert not math.isnan(total_err(suggested))


# ── top-up short runs (supplemental frequency for UNDER plants) ──────


def test_no_topup_for_ok_or_over_plants():
    loop = Loop("L", 30, 3)
    res = [
        _result("fine", 10.0, 10.0, 2.0, STATUS_OK),
        _result("flooded", 10.0, 15.0, 2.0, STATUS_OVER),
    ]
    assert recommend_topups(loop, res) == []


def test_feasible_topup_closes_deficit():
    # need 20, main delivers 14 (UNDER by 6); loop runs 3x/wk -> 4 extra runs to
    # reach daily; at 2 GPH, 45 min/run delivers exactly the 6 gal deficit.
    loop = Loop("L", 30, 3)
    plans = recommend_topups(loop, [_result("thirsty", 20.0, 14.0, 2.0, STATUS_UNDER)])
    assert len(plans) == 1
    p = plans[0]
    assert p.plant_name == "thirsty"
    assert p.deficit_gal_week == 6.0
    assert p.extra_runs_per_week == 4  # 7 - 3
    assert p.extra_minutes == 45
    assert p.feasible is True
    assert p.delivered_gal_week_after == pytest.approx(20.0, abs=0.2)


def test_topup_infeasible_when_already_daily():
    # Loop already runs daily -> added frequency can't help; flag own-loop.
    loop = Loop("L", 30, 7)
    plans = recommend_topups(loop, [_result("x", 20.0, 14.0, 2.0, STATUS_UNDER)])
    assert len(plans) == 1
    assert plans[0].extra_runs_per_week == 0
    assert plans[0].feasible is False


def test_topup_infeasible_when_deficit_too_large():
    # A giant deficit can't be closed by frequency within the minute cap.
    loop = Loop("L", 30, 3)
    plans = recommend_topups(loop, [_result("giant", 1000.0, 10.0, 2.0, STATUS_UNDER)])
    assert plans[0].extra_runs_per_week == 4
    assert plans[0].feasible is False
    assert plans[0].extra_minutes == TOPUP_MAX_MINUTES  # clamped, still short
    assert plans[0].delivered_gal_week_after < 1000.0


def test_topup_reports_overwater_cost_to_coplants():
    # Topping up the thirsty plant runs the whole loop, watering its co-plant too.
    loop = Loop("L", 30, 3)
    res = [
        _result("thirsty", 20.0, 14.0, 2.0, STATUS_UNDER),
        _result("neighbor", 12.0, 12.0, 1.0, STATUS_OK),
    ]
    plans = recommend_topups(loop, res)
    assert len(plans) == 1
    over = dict(plans[0].overwater)
    assert "neighbor" in over
    # 4 extra runs x 1 GPH x 45/60 h = 3 gal extra on a 12-gal need = +25%.
    assert over["neighbor"] == pytest.approx(0.25, abs=0.02)


def test_build_loop_report_attaches_topups():
    # End-to-end: a thirsty plant on a short, infrequent loop yields a top-up.
    loop = Loop("zone.shrubs", 10, 2)
    plants = [_plant("high", 600.0), _plant("very_low", 20.0)]
    rep = build_loop_report(loop, plants, eto=2.0, efficiency=0.9)
    under = [r for r in rep.plants if r.status == STATUS_UNDER]
    assert under, "expected at least one UNDER plant for this stressed loop"
    assert len(rep.topups) == len(under)
    assert all(t.plant_name for t in rep.topups)
