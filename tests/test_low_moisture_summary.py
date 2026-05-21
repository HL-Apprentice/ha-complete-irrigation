"""Tests for compute_low_moisture_offenders — pure logic, no HA.

The coordinator's daily low-moisture summary feature is tested here
in its decomposed form: a list of per-zone sensor offenders given
(zones config, state-getter callable).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from custom_components.complete_irrigation.coordinator import (
    compute_low_moisture_offenders,
)


@dataclass
class _FakeState:
    state: str
    attributes: dict = field(default_factory=dict)


def _states(*pairs: tuple[str, _FakeState | None]) -> dict[str, _FakeState | None]:
    return dict(pairs)


def _getter(state_map):
    return lambda eid: state_map.get(eid)


# ── happy / unhappy paths ────────────────────────────────────────────


def test_empty_zones_returns_empty():
    assert compute_low_moisture_offenders({}, lambda _: None) == []


def test_zone_with_no_sensors_skipped():
    zones = {"switch.lawn": {"min_pct": 21}}
    assert compute_low_moisture_offenders(zones, lambda _: None) == []


def test_zone_with_no_min_pct_skipped():
    zones = {"switch.lawn": {"moisture_entities": ["sensor.lawn_moisture"]}}
    sm = _states(("sensor.lawn_moisture", _FakeState("10")))
    assert compute_low_moisture_offenders(zones, _getter(sm)) == []


def test_zone_above_min_no_offender():
    zones = {
        "switch.lawn": {
            "moisture_entities": ["sensor.lawn_moisture"],
            "min_pct": 21,
        }
    }
    sm = _states(("sensor.lawn_moisture", _FakeState("35")))
    assert compute_low_moisture_offenders(zones, _getter(sm)) == []


def test_zone_at_min_no_offender():
    """min_pct is a strict less-than threshold — equal is fine."""
    zones = {
        "switch.lawn": {
            "moisture_entities": ["sensor.lawn_moisture"],
            "min_pct": 21,
        }
    }
    sm = _states(("sensor.lawn_moisture", _FakeState("21")))
    assert compute_low_moisture_offenders(zones, _getter(sm)) == []


def test_zone_below_min_appears_in_offenders():
    zones = {
        "switch.lawn": {
            "moisture_entities": ["sensor.lawn_moisture"],
            "min_pct": 21,
        }
    }
    sm = _states(
        (
            "sensor.lawn_moisture",
            _FakeState("18", attributes={"friendly_name": "Lawn Moisture"}),
        )
    )
    out = compute_low_moisture_offenders(zones, _getter(sm))
    assert len(out) == 1
    assert "switch.lawn" in out[0]
    assert "Lawn Moisture" in out[0]
    assert "18.0%" in out[0]
    assert "min 21" in out[0]


def test_friendly_name_falls_back_to_entity_id():
    zones = {"switch.lawn": {"moisture_entities": ["sensor.x"], "min_pct": 30}}
    sm = _states(("sensor.x", _FakeState("10")))
    out = compute_low_moisture_offenders(zones, _getter(sm))
    assert "sensor.x" in out[0]


# ── multi-sensor: one offender per zone ──────────────────────────────


def test_multiple_low_sensors_one_zone_yields_one_offender():
    """Should report each zone at most once, even if every sensor is below."""
    zones = {
        "switch.lawn": {
            "moisture_entities": ["sensor.a", "sensor.b", "sensor.c"],
            "min_pct": 21,
        }
    }
    sm = _states(
        ("sensor.a", _FakeState("10")),
        ("sensor.b", _FakeState("12")),
        ("sensor.c", _FakeState("8")),
    )
    out = compute_low_moisture_offenders(zones, _getter(sm))
    assert len(out) == 1


def test_first_low_sensor_wins_in_iteration_order():
    zones = {
        "switch.lawn": {
            "moisture_entities": ["sensor.a", "sensor.b"],
            "min_pct": 21,
        }
    }
    sm = _states(
        ("sensor.a", _FakeState("25")),  # OK
        ("sensor.b", _FakeState("18", attributes={"friendly_name": "B"})),  # low
    )
    out = compute_low_moisture_offenders(zones, _getter(sm))
    assert "B" in out[0]


# ── unavailable / missing / bad data ─────────────────────────────────


def test_missing_state_skipped():
    zones = {"switch.lawn": {"moisture_entities": ["sensor.gone"], "min_pct": 21}}
    assert compute_low_moisture_offenders(zones, lambda _: None) == []


def test_unavailable_state_skipped():
    zones = {"switch.lawn": {"moisture_entities": ["sensor.x"], "min_pct": 21}}
    sm = _states(("sensor.x", _FakeState("unavailable")))
    assert compute_low_moisture_offenders(zones, _getter(sm)) == []


def test_unknown_state_skipped():
    zones = {"switch.lawn": {"moisture_entities": ["sensor.x"], "min_pct": 21}}
    sm = _states(("sensor.x", _FakeState("unknown")))
    assert compute_low_moisture_offenders(zones, _getter(sm)) == []


def test_non_numeric_state_skipped():
    zones = {"switch.lawn": {"moisture_entities": ["sensor.x"], "min_pct": 21}}
    sm = _states(("sensor.x", _FakeState("not a number")))
    assert compute_low_moisture_offenders(zones, _getter(sm)) == []


def test_multiple_zones_each_reported_independently():
    zones = {
        "switch.lawn": {"moisture_entities": ["sensor.lawn"], "min_pct": 21},
        "switch.garden": {"moisture_entities": ["sensor.garden"], "min_pct": 30},
        "switch.trees": {"moisture_entities": ["sensor.trees"], "min_pct": 18},
    }
    sm = _states(
        ("sensor.lawn", _FakeState("10")),  # low
        ("sensor.garden", _FakeState("45")),  # OK
        ("sensor.trees", _FakeState("12")),  # low
    )
    out = compute_low_moisture_offenders(zones, _getter(sm))
    assert len(out) == 2
    assert any("switch.lawn" in o for o in out)
    assert any("switch.trees" in o for o in out)
    assert not any("switch.garden" in o for o in out)
