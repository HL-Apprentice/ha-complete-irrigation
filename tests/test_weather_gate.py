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

# ── rain lockout tiers (defaults from const.py) ────────────────────


def test_no_rain_means_no_lockout():
    assert evaluate_rain_lockout(rainfall_inches_6h=0.0) is None
    assert evaluate_rain_lockout(rainfall_inches_6h=0.09) is None


def test_light_rain_triggers_4_hour_lockout():
    h = evaluate_rain_lockout(rainfall_inches_6h=0.10)
    assert h == 4
    assert evaluate_rain_lockout(rainfall_inches_6h=0.20) == 4


def test_moderate_rain_triggers_6_hour_lockout():
    assert evaluate_rain_lockout(rainfall_inches_6h=0.25) == 6
    assert evaluate_rain_lockout(rainfall_inches_6h=0.40) == 6


def test_heavy_rain_triggers_12_hour_lockout():
    assert evaluate_rain_lockout(rainfall_inches_6h=0.50) == 12
    assert evaluate_rain_lockout(rainfall_inches_6h=0.90) == 12


def test_very_heavy_rain_triggers_24_hour_lockout():
    assert evaluate_rain_lockout(rainfall_inches_6h=1.00) == 24
    assert evaluate_rain_lockout(rainfall_inches_6h=2.50) == 24


def test_custom_tiers_override_defaults():
    custom = [(0.05, 2), (0.20, 8)]
    assert evaluate_rain_lockout(rainfall_inches_6h=0.04, tiers=custom) is None
    assert evaluate_rain_lockout(rainfall_inches_6h=0.05, tiers=custom) == 2
    assert evaluate_rain_lockout(rainfall_inches_6h=0.19, tiers=custom) == 2
    assert evaluate_rain_lockout(rainfall_inches_6h=0.20, tiers=custom) == 8


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
