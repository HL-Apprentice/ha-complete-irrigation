"""Tests for MoistureGate (pure logic).

4-band controller per the design (saturated / light / normal / urgent),
plus sensor combine modes.
"""

from __future__ import annotations

import pytest

from custom_components.complete_irrigation.moisture_gate import (
    BAND_LIGHT,
    BAND_NORMAL,
    BAND_SATURATED,
    BAND_URGENT,
    COMBINE_AVERAGE,
    COMBINE_HIGHEST,
    COMBINE_LOWEST,
    COMBINE_PRIMARY,
    combine_moisture,
    evaluate_moisture,
)

# ── 4-band evaluator ──────────────────────────────────────────────


def test_above_max_skips_as_saturated():
    d = evaluate_moisture(current=45, min_pct=21, target=31, max_pct=40, base_minutes=20)
    assert d.band == BAND_SATURATED
    assert d.runtime_minutes == 0
    assert d.skip is True


def test_at_max_skips():
    d = evaluate_moisture(current=40, min_pct=21, target=31, max_pct=40, base_minutes=20)
    assert d.band == BAND_SATURATED
    assert d.skip is True


def test_above_target_but_below_max_light_run():
    d = evaluate_moisture(current=35, min_pct=21, target=31, max_pct=40, base_minutes=20)
    assert d.band == BAND_LIGHT
    assert d.skip is False
    # Light run is reduced from base
    assert 0 < d.runtime_minutes < 20


def test_at_target_normal_run():
    d = evaluate_moisture(current=31, min_pct=21, target=31, max_pct=40, base_minutes=20)
    assert d.band == BAND_LIGHT  # at target = upper half
    assert d.runtime_minutes >= 1


def test_below_target_normal_run():
    d = evaluate_moisture(current=28, min_pct=21, target=31, max_pct=40, base_minutes=20)
    assert d.band == BAND_NORMAL
    assert d.runtime_minutes == 20


def test_below_min_urgent_extends_runtime():
    d = evaluate_moisture(current=15, min_pct=21, target=31, max_pct=40, base_minutes=20)
    assert d.band == BAND_URGENT
    assert d.runtime_minutes > 20  # boosted


def test_evaluate_includes_human_reason():
    d = evaluate_moisture(current=15, min_pct=21, target=31, max_pct=40, base_minutes=20)
    assert "below" in d.reason.lower() or "urgent" in d.reason.lower()


# ── sensor combine modes ─────────────────────────────────────────


def test_average_combine():
    assert combine_moisture([30, 40, 50], mode=COMBINE_AVERAGE) == 40.0


def test_lowest_combine_picks_min():
    assert combine_moisture([30, 40, 50], mode=COMBINE_LOWEST) == 30


def test_highest_combine_picks_max():
    assert combine_moisture([30, 40, 50], mode=COMBINE_HIGHEST) == 50


def test_primary_combine_uses_first_value():
    """Primary mode: callers pass [primary_value, *others] — use [0]."""
    assert combine_moisture([30, 40, 50], mode=COMBINE_PRIMARY) == 30


def test_empty_readings_returns_none():
    assert combine_moisture([], mode=COMBINE_AVERAGE) is None


def test_unknown_mode_raises():
    with pytest.raises(ValueError):
        combine_moisture([30], mode="invented")
