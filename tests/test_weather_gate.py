"""Tests for WeatherGate.evaluate_rain_lockout (pure logic).

Given the recent + current rainfall, return the lockout duration tier
that should apply.
"""

from __future__ import annotations

from custom_components.complete_irrigation.weather_gate import (
    HotWeatherDecision,
    evaluate_hot_weather,
    evaluate_rain_lockout,
)

# ── rain lockout: ET-scaled formula (4-model consensus 2026-07) ────
# effective = max(0, rain - 0.10); below floor 0.10 or 0-effective -> no lockout;
# hours = clamp(round(effective / daily_eto * 24), 3, 48). Summer daily_eto ~0.30,
# winter ~0.06; fallback when None ~0.25.
SUMMER = 0.30
WINTER = 0.06


def test_trace_and_sub_floor_rain_never_lock_out():
    # The reported bug: a trace / small monsoon cell must NEVER lock out.
    assert evaluate_rain_lockout(0.01, SUMMER) is None
    assert evaluate_rain_lockout(0.05, SUMMER) is None
    assert evaluate_rain_lockout(0.09, SUMMER) is None
    assert evaluate_rain_lockout(0.10, SUMMER) is None  # at floor but all intercepted


def test_effective_rainfall_scales_with_live_eto():
    # 0.25 in -> effective 0.15. Summer: 0.15/0.30*24 = 12h. Winter: 0.15/0.06*24 = 60 -> cap 48h.
    assert evaluate_rain_lockout(0.25, SUMMER) == 12
    assert evaluate_rain_lockout(0.25, WINTER) == 48
    # 0.50 in -> effective 0.40. Summer: 0.40/0.30*24 = 32h. Winter capped 48h.
    assert evaluate_rain_lockout(0.50, SUMMER) == 32
    assert evaluate_rain_lockout(0.50, WINTER) == 48


def test_big_rain_clamps_to_max_48():
    assert evaluate_rain_lockout(1.0, SUMMER) == 48
    assert evaluate_rain_lockout(2.0, SUMMER) == 48
    assert evaluate_rain_lockout(2.0, WINTER) == 48


def test_min_hours_floor_on_tiny_effective():
    # 0.12 in -> effective 0.02. Summer: 0.02/0.30*24 = 1.6 -> round 2 -> clamped up to min 3h.
    assert evaluate_rain_lockout(0.12, SUMMER) == 3


def test_missing_eto_uses_short_fallback():
    # daily_eto None -> fallback 0.25 (biases short, errs toward watering).
    # 0.50 in -> effective 0.40 / 0.25 * 24 = 38.4 -> 38h.
    assert evaluate_rain_lockout(0.50, None) == 38
    # A zero/negative/NaN eto also falls back.
    assert evaluate_rain_lockout(0.50, 0.0) == 38
    assert evaluate_rain_lockout(0.50, float("nan")) == 38


def test_floor_and_interception_are_configurable():
    # Raise the floor (user's summer preference): 0.4 in no longer locks out.
    assert evaluate_rain_lockout(0.40, SUMMER, min_inches=0.5) is None
    assert evaluate_rain_lockout(0.50, SUMMER, min_inches=0.5) == 32


def test_nan_rainfall_never_locks_out():
    assert evaluate_rain_lockout(float("nan"), SUMMER) is None
    assert evaluate_rain_lockout(float("inf"), SUMMER) is None


# ── hot weather boost ──────────────────────────────────────────────


def test_below_threshold_no_boost():
    d = evaluate_hot_weather(daily_high_f=85, threshold_f=100, boost_percent=25)
    assert d == HotWeatherDecision(boost=False, multiplier=1.0)


def test_at_threshold_applies_boost():
    d = evaluate_hot_weather(daily_high_f=100, threshold_f=100, boost_percent=25)
    assert d.boost is True
    assert d.multiplier == 1.25


def test_well_above_threshold_applies_same_boost():
    """Boost is a single step, not a curve — same multiplier above threshold."""
    d = evaluate_hot_weather(daily_high_f=110, threshold_f=100, boost_percent=25)
    assert d.multiplier == 1.25


def test_zero_boost_disabled():
    d = evaluate_hot_weather(daily_high_f=110, threshold_f=100, boost_percent=0)
    assert d == HotWeatherDecision(boost=False, multiplier=1.0)


def test_custom_boost_percent():
    d = evaluate_hot_weather(daily_high_f=105, threshold_f=100, boost_percent=50)
    assert d.multiplier == 1.5


def test_hot_day_cap_never_exceeds_24h():
    """Heat-aware cap — on a hot day a big rain must still resume within ~1 day
    (112F + no water >1 day stresses plants; baked desert soil sheds rain)."""
    # Summer ETo, big rain: normal cap 48h, but hot-day max_hours=24 clamps it.
    assert evaluate_rain_lockout(1.0, 0.30, max_hours=24) == 24
    assert evaluate_rain_lockout(2.0, 0.30, max_hours=24) == 24
    # Small rain still scales below the cap unaffected.
    assert evaluate_rain_lockout(0.25, 0.30, max_hours=24) == 12
    # Off a hot day (max 48) the same big rain may lock out longer.
    assert evaluate_rain_lockout(1.0, 0.30, max_hours=48) == 48
