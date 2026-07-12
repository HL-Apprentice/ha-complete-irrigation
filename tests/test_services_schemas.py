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
    _looks_like_image,
)

# ── _looks_like_image: stored-file XSS guard (v1.32 security gate) ────


def test_looks_like_image_accepts_real_formats():
    # Minimal valid magic-byte headers for each accepted format.
    assert _looks_like_image(b"\xff\xd8\xff" + b"\x00" * 20)  # JPEG
    assert _looks_like_image(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)  # PNG
    assert _looks_like_image(b"GIF89a" + b"\x00" * 20)  # GIF
    assert _looks_like_image(b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 8)  # WEBP
    assert _looks_like_image(b"BM" + b"\x00" * 20)  # BMP


def test_looks_like_image_rejects_xss_payloads():
    # A crafted SVG/HTML payload (the stored-XSS vector) must be rejected so it
    # can't be written as .jpg and served from /local.
    for payload in (
        b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
        b"<!DOCTYPE html><html><script>alert(1)</script></html>",
        b"<?xml version='1.0'?><svg/>",
        b"GIF",  # too short / truncated
        b"",
        b"\x00" * 11,  # under the 12-byte minimum
        b"plain text not an image at all",
    ):
        assert not _looks_like_image(payload)


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


def test_zone_moisture_schema_accepts_moisture_disabled():
    """v1.18.4 — per-zone 'ignore moisture for watering decisions'
    toggle. Sensors stay bound for display; the gate becomes a no-op."""
    from custom_components.complete_irrigation.services import (
        _SET_ZONE_MOISTURE_SCHEMA,
    )

    result = _SET_ZONE_MOISTURE_SCHEMA(
        {
            "zone_entity_id": "switch.garden",
            "moisture_entities": ["sensor.garden_moisture"],
            "moisture_disabled": True,
        }
    )
    assert result["moisture_disabled"] is True
    # Omitted → absent (gate reads falsy → enabled, the default)
    result = _SET_ZONE_MOISTURE_SCHEMA(
        {
            "zone_entity_id": "switch.garden",
            "moisture_entities": ["sensor.garden_moisture"],
        }
    )
    assert "moisture_disabled" not in result


def test_zone_moisture_schema_accepts_v119_fields():
    """v1.19 — per-sensor analysis exclusion + auto-soak recovery."""
    from custom_components.complete_irrigation.services import (
        _SET_ZONE_MOISTURE_SCHEMA,
    )

    result = _SET_ZONE_MOISTURE_SCHEMA(
        {
            "zone_entity_id": "switch.garden",
            "moisture_entities": ["sensor.a_moisture", "sensor.b_moisture"],
            "moisture_excluded": ["sensor.b_moisture"],
            "auto_soak_enabled": True,
            "soak_run_minutes": 10,
            "soak_wait_minutes": 30,
            "soak_max_cycles": 4,
        }
    )
    assert result["moisture_excluded"] == ["sensor.b_moisture"]
    assert result["auto_soak_enabled"] is True
    assert result["soak_run_minutes"] == 10
    assert result["soak_wait_minutes"] == 30
    assert result["soak_max_cycles"] == 4


def test_zone_moisture_schema_rejects_out_of_range_soak_fields():
    from custom_components.complete_irrigation.services import (
        _SET_ZONE_MOISTURE_SCHEMA,
    )

    base = {
        "zone_entity_id": "switch.garden",
        "moisture_entities": ["sensor.a_moisture"],
    }
    with pytest.raises(vol.Invalid):
        _SET_ZONE_MOISTURE_SCHEMA({**base, "soak_run_minutes": 0})
    with pytest.raises(vol.Invalid):
        _SET_ZONE_MOISTURE_SCHEMA({**base, "soak_wait_minutes": 2})
    with pytest.raises(vol.Invalid):
        _SET_ZONE_MOISTURE_SCHEMA({**base, "soak_max_cycles": 99})


def test_run_schedule_schema_requires_schedule_id():
    """v1.17.13 — run_schedule needs exactly one field: schedule_id."""
    from custom_components.complete_irrigation.services import _RUN_SCHEDULE_SCHEMA

    # Valid
    assert _RUN_SCHEDULE_SCHEMA({"schedule_id": "abc123"}) == {"schedule_id": "abc123"}
    # Missing required field
    with pytest.raises(vol.Invalid):
        _RUN_SCHEDULE_SCHEMA({})


def test_general_config_schema_accepts_admin_only_services():
    """v1.17.11 — `admin_only_services` is the opt-in flag that gates
    every hardware/CRUD service handler on caller's admin status."""
    from custom_components.complete_irrigation.services import (
        _SET_GENERAL_CONFIG_SCHEMA,
    )

    result = _SET_GENERAL_CONFIG_SCHEMA({"admin_only_services": True})
    assert result["admin_only_services"] is True

    result = _SET_GENERAL_CONFIG_SCHEMA({"admin_only_services": False})
    assert result["admin_only_services"] is False


def test_general_config_schema_accepts_chunking_keys():
    """v1.25 — controller_max_run_minutes (per-activation block size) +
    block_gap_seconds tune the long-run chunking. Bounded so a typo can't set an
    absurd value that would starve or hammer the controller."""
    from custom_components.complete_irrigation.services import (
        _SET_GENERAL_CONFIG_SCHEMA,
    )

    result = _SET_GENERAL_CONFIG_SCHEMA({"controller_max_run_minutes": 55, "block_gap_seconds": 45})
    assert result["controller_max_run_minutes"] == 55
    assert result["block_gap_seconds"] == 45
    with pytest.raises(vol.Invalid):  # over the 480 ceiling
        _SET_GENERAL_CONFIG_SCHEMA({"controller_max_run_minutes": 999})
    with pytest.raises(vol.Invalid):  # under the 5s floor
        _SET_GENERAL_CONFIG_SCHEMA({"block_gap_seconds": 1})


def test_notification_schema_accepts_notify_on_aborted():
    """v1.17.8 — `notify_on_aborted` joins the notifications config
    blob alongside notify_on_missed + low_moisture_alerts. Validates
    cleanly when the panel includes it in the save payload."""
    from custom_components.complete_irrigation.services import (
        _SET_NOTIFICATION_CONFIG_SCHEMA,
    )

    result = _SET_NOTIFICATION_CONFIG_SCHEMA(
        {
            "notify_on_aborted": False,
            "notify_on_missed": True,
        }
    )
    assert result["notify_on_aborted"] is False
    assert result["notify_on_missed"] is True


def test_notification_config_validates_quiet_hours_format():
    """A malformed quiet-hours value must be rejected at the schema boundary so it
    can't reach the parse sinks and suppress alerts / break startup."""
    from custom_components.complete_irrigation.services import (
        _SET_NOTIFICATION_CONFIG_SCHEMA,
    )

    ok = _SET_NOTIFICATION_CONFIG_SCHEMA({"quiet_hours_start": "22:00", "quiet_hours_end": "07:00"})
    assert ok["quiet_hours_start"] == "22:00"
    for bad in ("", "nope", "25:00", "12:99", "8am", "22:0"):
        with pytest.raises(vol.Invalid):
            _SET_NOTIFICATION_CONFIG_SCHEMA({"quiet_hours_start": bad})


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


# ── v2 plant CRUD schemas ───────────────────────────────────────────


def test_add_plant_schema_accepts_valid():
    from custom_components.complete_irrigation.services import _ADD_PLANT_SCHEMA

    result = _ADD_PLANT_SCHEMA(
        {
            "name": "Lemon tree",
            "wucols_category": "high",
            "canopy_area_sqft": 113,
            "zone_entity_id": "switch.citrus",
        }
    )
    assert result["wucols_category"] == "high"
    assert result["canopy_area_sqft"] == 113.0  # coerced to float


def test_add_plant_schema_rejects_bad_category_and_area():
    from custom_components.complete_irrigation.services import _ADD_PLANT_SCHEMA

    base = {"name": "X", "canopy_area_sqft": 10, "zone_entity_id": "switch.z"}
    with pytest.raises(vol.Invalid):
        _ADD_PLANT_SCHEMA({**base, "wucols_category": "ultra"})
    with pytest.raises(vol.Invalid):
        _ADD_PLANT_SCHEMA({**base, "wucols_category": "low", "canopy_area_sqft": 0})


def test_update_plant_schema_allows_partial():
    from custom_components.complete_irrigation.services import _UPDATE_PLANT_SCHEMA

    result = _UPDATE_PLANT_SCHEMA({"plant_id": "abc123", "canopy_area_sqft": 50})
    assert result["plant_id"] == "abc123"
    assert result["canopy_area_sqft"] == 50.0
    assert "name" not in result


def test_delete_plant_schema_requires_id():
    from custom_components.complete_irrigation.services import _DELETE_PLANT_SCHEMA

    assert _DELETE_PLANT_SCHEMA({"plant_id": "abc"}) == {"plant_id": "abc"}
    with pytest.raises(vol.Invalid):
        _DELETE_PLANT_SCHEMA({})


def test_run_zone_schema_accepts_long_scheduled_durations():
    """v1.24 — the run_zone 'minutes' cap was 60, which silently rejected every
    scheduled run over 60 min (the long deep-watering zones never fired). It now
    accepts up to the schedule cap (480 min)."""
    from custom_components.complete_irrigation.services import _RUN_ZONE_SCHEMA

    for mins in (135, 240, 300, 480):  # Shrubs / Citrus / Trees / max
        result = _RUN_ZONE_SCHEMA({"entity_id": "switch.front_back_trees", "minutes": mins})
        assert result["minutes"] == float(mins)
    with pytest.raises(vol.Invalid):  # still bounded — no absurd values
        _RUN_ZONE_SCHEMA({"entity_id": "switch.x", "minutes": 999})


def test_add_care_task_schema_zone_entity_validation():
    """v1.35 review fix — zone_entity_id must be empty or a real entity-id format
    (it is echoed into notifications), never an arbitrary string."""
    import voluptuous as vol

    from custom_components.complete_irrigation.services import _ADD_CARE_TASK_SCHEMA

    base = {"kind": "prune", "interval_days": 30}
    ok = _ADD_CARE_TASK_SCHEMA({**base, "zone_entity_id": "switch.trees"})
    assert ok["zone_entity_id"] == "switch.trees"
    ok_empty = _ADD_CARE_TASK_SCHEMA({**base, "plant_id": "abc"})
    assert ok_empty["zone_entity_id"] == ""
    with pytest.raises(vol.Invalid):
        _ADD_CARE_TASK_SCHEMA({**base, "zone_entity_id": "not an entity\nid"})


def test_care_task_store_for_plant_supports_cascade_delete():
    """v1.35 review fix — delete_plant cascades via for_plant(); lock its contract."""
    from custom_components.complete_irrigation.care_tasks import CareTask, CareTaskStore

    store = CareTaskStore()
    store.add(CareTask(id="a1", kind="fertilize", interval_days=30, plant_id="p1"))
    store.add(CareTask(id="a2", kind="prune", interval_days=60, plant_id="p1"))
    store.add(CareTask(id="a3", kind="mulch", interval_days=90, zone_entity_id="switch.x"))
    mine = store.for_plant("p1")
    assert {t.id for t in mine} == {"a1", "a2"}
    for t in mine:
        assert store.delete(t.id)
    assert [t.id for t in store.all()] == ["a3"]


def test_every_service_registration_has_exactly_three_positional_args():
    """v1.39.2 regression lock — a stray 4th positional arg in async_register
    (schema slot) raises TypeError AT SETUP and kills half the integration
    (sidebar/views gone, coordinator still ticking). Nothing executes the
    registration function in tests, so lock the call SHAPE via AST instead."""
    import ast
    import pathlib

    src = pathlib.Path("custom_components/complete_irrigation/services.py").read_text()
    tree = ast.parse(src)
    bad = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "async_register"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "services"
        ):
            if len(node.args) != 3:
                bad.append(f"line {node.lineno}: {len(node.args)} positional args")
    assert not bad, f"async_register calls must have exactly 3 positional args: {bad}"
