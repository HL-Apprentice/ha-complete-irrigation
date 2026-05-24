"""Tests for the voluptuous schemas in services.py.

We import the schema constants directly and run them like any other
pure-logic function. Catches schema regressions (the "must be at
least length 1" trap caught users in v1.17.5) before they reach the
panel.

Skipped on dev machines without HA installed — services.py imports
from homeassistant.* at module-load time. Runs in CI where HA is
present (and in any contributor's local env with HA installed).
"""

from __future__ import annotations

import pytest

vol = pytest.importorskip("voluptuous")
pytest.importorskip("homeassistant.const")

from custom_components.complete_irrigation.services import (  # noqa: E402
    _ADD_SCHEDULE_SCHEMA,
    _UPDATE_SCHEDULE_SCHEMA,
    _WEEKDAYS_SCHEMA,
)

# ── _WEEKDAYS_SCHEMA ─────────────────────────────────────────────────


def test_weekdays_schema_accepts_empty_list():
    """v1.17.5 regression: interval / interval_hours updates send
    weekdays=[] because the field isn't applicable in those modes.
    The schema must accept it; mode-specific validation happens at
    Schedule.__post_init__ via _validate_weekdays_mode()."""
    assert _WEEKDAYS_SCHEMA([]) == []


def test_weekdays_schema_accepts_valid_iso_weekdays():
    assert _WEEKDAYS_SCHEMA([0, 1, 2, 3, 4, 5, 6]) == [0, 1, 2, 3, 4, 5, 6]


def test_weekdays_schema_coerces_strings_to_ints():
    assert _WEEKDAYS_SCHEMA(["0", "3", "6"]) == [0, 3, 6]


def test_weekdays_schema_rejects_out_of_range():
    with pytest.raises(vol.Invalid):
        _WEEKDAYS_SCHEMA([7])
    with pytest.raises(vol.Invalid):
        _WEEKDAYS_SCHEMA([-1])


# ── _UPDATE_SCHEDULE_SCHEMA — the bug surface ───────────────────────


def test_update_schedule_with_empty_weekdays_for_interval_hours_mode():
    """Reproduces the exact failure mode user reported in v1.17.5:
    Bird Bath is interval_hours mode, panel sends weekdays=[] alongside
    the new ignore_* toggles, schema previously rejected with
    "length of value must be at least 1 for dictionary value @
    data['weekdays']".

    After v1.17.5: empty weekdays is accepted on UPDATE; the dataclass
    constructor still enforces "must have weekdays if mode=weekdays"
    via _validate_weekdays_mode."""
    result = _UPDATE_SCHEDULE_SCHEMA(
        {
            "schedule_id": "abc123",
            "name": "Bird Bath",
            "weekdays": [],
            "mode": "interval_hours",
            "interval_hours": 4,
            "interval_anchor": "2026-05-23",
            "ignore_wind": True,
            "ignore_hot_weather": True,
            "ignore_rain_lockout": True,
        }
    )
    assert result["weekdays"] == []
    assert result["ignore_wind"] is True
    assert result["interval_hours"] == 4


def test_update_schedule_with_weekdays_in_weekdays_mode():
    """The common-case path still works."""
    result = _UPDATE_SCHEDULE_SCHEMA(
        {
            "schedule_id": "xyz",
            "weekdays": [0, 1, 2, 3, 4],
            "mode": "weekdays",
        }
    )
    assert result["weekdays"] == [0, 1, 2, 3, 4]


def test_add_schedule_with_interval_hours_and_empty_weekdays_unchanged():
    """ADD has always used a permissive weekdays validator. Pinned here
    to make sure we don't accidentally tighten it later."""
    result = _ADD_SCHEDULE_SCHEMA(
        {
            "name": "Bird Bath",
            "zone_entity_id": "switch.bird_bath",
            "start_time": "06:00",
            "duration_minutes": 25,
            "mode": "interval_hours",
            "interval_hours": 4,
            "interval_anchor": "2026-05-23",
            "weekdays": [],
        }
    )
    assert result["weekdays"] == []
