"""Tests for v1.9.0 pure-logic additions.

• detect_sensor_offline_transitions (PRD #57)
• compute_expired_establishment_schedules (PRD #76)
• evaluate_wind_defer (PRD #52)
• suggest_category (PRD #20)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time

import pytest

from custom_components.complete_irrigation.category_match import suggest_category
from custom_components.complete_irrigation.coordinator import (
    compute_expired_establishment_schedules,
    detect_sensor_offline_transitions,
)
from custom_components.complete_irrigation.schedule import Schedule
from custom_components.complete_irrigation.weather_gate import (
    DEFAULT_WIND_DEFER_MPH,
    evaluate_wind_defer,
)


@dataclass
class _FakeState:
    state: str
    attributes: dict = field(default_factory=dict)


def _getter(state_map):
    return lambda eid: state_map.get(eid)


# ════════════════════════════════════════════════════════════════════
# PRD #57 — detect_sensor_offline_transitions
# ════════════════════════════════════════════════════════════════════


def test_sensor_offline_no_zones_returns_empty():
    a, b = detect_sensor_offline_transitions({}, lambda _: None, set())
    assert a == set()
    assert b == set()


def test_sensor_online_no_transitions():
    zones = {"switch.lawn": {"moisture_entities": ["sensor.lawn_m"]}}
    sm = {"sensor.lawn_m": _FakeState("25")}
    newly_off, newly_back = detect_sensor_offline_transitions(zones, _getter(sm), set())
    assert newly_off == set()
    assert newly_back == set()


def test_sensor_goes_unavailable_flagged_as_newly_offline():
    zones = {"switch.lawn": {"moisture_entities": ["sensor.lawn_m"]}}
    sm = {"sensor.lawn_m": _FakeState("unavailable")}
    newly_off, newly_back = detect_sensor_offline_transitions(zones, _getter(sm), set())
    assert newly_off == {"sensor.lawn_m"}
    assert newly_back == set()


def test_sensor_state_unknown_also_treated_as_offline():
    zones = {"switch.lawn": {"moisture_entities": ["sensor.lawn_m"]}}
    sm = {"sensor.lawn_m": _FakeState("unknown")}
    newly_off, _ = detect_sensor_offline_transitions(zones, _getter(sm), set())
    assert "sensor.lawn_m" in newly_off


def test_sensor_missing_from_hass_treated_as_offline():
    zones = {"switch.lawn": {"moisture_entities": ["sensor.gone"]}}
    newly_off, _ = detect_sensor_offline_transitions(zones, lambda _: None, set())
    assert newly_off == {"sensor.gone"}


def test_sensor_recovers_appears_in_newly_back_online():
    zones = {"switch.lawn": {"moisture_entities": ["sensor.lawn_m"]}}
    sm = {"sensor.lawn_m": _FakeState("30")}  # back online
    newly_off, newly_back = detect_sensor_offline_transitions(zones, _getter(sm), {"sensor.lawn_m"})
    assert newly_off == set()
    assert newly_back == {"sensor.lawn_m"}


def test_sensor_already_offline_not_re_reported():
    zones = {"switch.lawn": {"moisture_entities": ["sensor.lawn_m"]}}
    sm = {"sensor.lawn_m": _FakeState("unavailable")}
    newly_off, newly_back = detect_sensor_offline_transitions(zones, _getter(sm), {"sensor.lawn_m"})
    assert newly_off == set()  # already in tracked
    assert newly_back == set()


def test_multiple_sensors_one_offline_one_online():
    zones = {
        "switch.lawn": {"moisture_entities": ["sensor.a", "sensor.b"]},
        "switch.garden": {"moisture_entities": ["sensor.c"]},
    }
    sm = {
        "sensor.a": _FakeState("25"),
        "sensor.b": _FakeState("unavailable"),
        "sensor.c": _FakeState("18"),
    }
    newly_off, _ = detect_sensor_offline_transitions(zones, _getter(sm), set())
    assert newly_off == {"sensor.b"}


# ════════════════════════════════════════════════════════════════════
# PRD #76 — compute_expired_establishment_schedules
# ════════════════════════════════════════════════════════════════════


def _est_schedule(**overrides) -> Schedule:
    defaults = {
        "id": "est1",
        "name": "Front lawn establishment",
        "zone_entity_id": "switch.front_lawn",
        "start_time": time(6, 0),
        "duration_minutes": 10,
        "weekdays": (0, 1, 2, 3, 4, 5, 6),
        "end_date": date(2026, 5, 21),
    }
    defaults.update(overrides)
    return Schedule(**defaults)


def test_schedule_with_no_end_date_never_expires():
    s = _est_schedule(end_date=None)
    out = compute_expired_establishment_schedules([s], date(2026, 5, 30), set())
    assert out == []


def test_schedule_end_date_in_future_not_expired():
    s = _est_schedule(end_date=date(2026, 5, 30))
    out = compute_expired_establishment_schedules([s], date(2026, 5, 25), set())
    assert out == []


def test_schedule_end_date_today_appears_in_expired():
    s = _est_schedule(end_date=date(2026, 5, 25))
    out = compute_expired_establishment_schedules([s], date(2026, 5, 25), set())
    assert out == [s]


def test_schedule_end_date_past_appears_in_expired():
    s = _est_schedule(end_date=date(2026, 5, 20))
    out = compute_expired_establishment_schedules([s], date(2026, 5, 25), set())
    assert out == [s]


def test_already_prompted_schedule_not_re_reported():
    s = _est_schedule(end_date=date(2026, 5, 20))
    out = compute_expired_establishment_schedules([s], date(2026, 5, 25), {s.id})
    assert out == []


def test_multiple_schedules_only_expired_returned():
    a = _est_schedule(id="a", end_date=date(2026, 5, 20))  # expired
    b = _est_schedule(id="b", end_date=date(2026, 5, 30))  # future
    c = _est_schedule(id="c", end_date=None)  # no end
    d = _est_schedule(id="d", end_date=date(2026, 5, 21))  # expired
    out = compute_expired_establishment_schedules([a, b, c, d], date(2026, 5, 25), set())
    ids = {s.id for s in out}
    assert ids == {"a", "d"}


# ════════════════════════════════════════════════════════════════════
# PRD #52 — evaluate_wind_defer
# ════════════════════════════════════════════════════════════════════


def test_wind_defer_no_wind_data_returns_false():
    assert evaluate_wind_defer(None, 15.0) is False


def test_wind_defer_below_threshold_returns_false():
    assert evaluate_wind_defer(5.0, 15.0) is False
    assert evaluate_wind_defer(14.9, 15.0) is False


def test_wind_defer_at_or_above_threshold_returns_true():
    assert evaluate_wind_defer(15.0, 15.0) is True
    assert evaluate_wind_defer(25.0, 15.0) is True


def test_wind_defer_zero_threshold_means_disabled():
    """Threshold of 0 should never defer (matches the 'off' UX)."""
    assert evaluate_wind_defer(100.0, 0.0) is False


def test_wind_defer_handles_string_input():
    """HA sensor states arrive as strings; the helper coerces them."""
    assert evaluate_wind_defer("20", 15.0) is True
    assert evaluate_wind_defer("not a number", 15.0) is False


def test_wind_defer_default_threshold_is_reasonable():
    """Sanity: the default is in the typical 'drift' range, not 0 or huge."""
    assert 10.0 <= DEFAULT_WIND_DEFER_MPH <= 25.0


# ════════════════════════════════════════════════════════════════════
# PRD #20 — suggest_category from zone name
# ════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Front Lawn", "lawn"),
        ("Back yard grass", "lawn"),
        ("Side turf", "lawn"),
        ("Vegetable Garden", "vegetable_garden"),
        ("Tomato bed", "vegetable_garden"),
        ("Herb planter", "vegetable_garden"),
        ("Shrub line", "bushes"),
        ("Hedge row", "bushes"),
        ("Citrus drip", "citrus"),
        ("Lemon tree zone", "citrus"),  # matches 'lemon' (citrus) before 'tree'
        ("Oak watering", "trees"),
        ("Random zone name", None),
        ("", None),
    ],
)
def testsuggest_category_matches_keyword(name, expected):
    assert suggest_category(name) == expected


def testsuggest_category_case_insensitive():
    assert suggest_category("LAWN") == "lawn"
    assert suggest_category("Lawn") == "lawn"
