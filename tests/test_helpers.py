"""Tests for the controller detection helper.

Per the tdd skill: tests verify behavior through public interfaces, not
implementation details. The helper is a pure function — given a list of
installed HA integration domains plus a mapping of known irrigation
domains, return the irrigation matches in deterministic order.
"""

from __future__ import annotations

from custom_components.complete_irrigation.const import KNOWN_IRRIGATION_DOMAINS
from custom_components.complete_irrigation.helpers import (
    detect_irrigation_integrations,
    select_zone_candidates,
)

# ─── happy paths ─────────────────────────────────────────────────────


def test_no_integrations_installed_returns_empty():
    assert detect_irrigation_integrations(installed_domains=[]) == []


def test_only_non_irrigation_integrations_returns_empty():
    assert (
        detect_irrigation_integrations(installed_domains=["zha", "mqtt", "weatherflow", "calendar"])
        == []
    )


def test_single_rachio_installation_returns_rachio():
    result = detect_irrigation_integrations(installed_domains=["rachio"])
    assert result == [("rachio", "Rachio (official)")]


def test_multiple_irrigation_integrations_all_returned():
    result = detect_irrigation_integrations(
        installed_domains=["rachio", "hydrawise", "rainmachine", "mqtt", "zha"]
    )
    domains = [d for d, _ in result]
    assert set(domains) == {"rachio", "hydrawise", "rainmachine"}
    assert len(result) == 3


# ─── sorting + determinism ───────────────────────────────────────────


def test_result_order_is_stable_alphabetical_by_domain():
    """Same input → same output, always — order matters for the UI list."""
    result = detect_irrigation_integrations(
        installed_domains=["rainmachine", "rachio", "hydrawise"]
    )
    domains = [d for d, _ in result]
    assert domains == sorted(domains), f"expected alphabetical, got {domains}"


# ─── deduplication ───────────────────────────────────────────────────


def test_duplicate_installed_domains_deduplicated():
    """HA could in theory report a domain twice (multiple config entries
    for the same domain — e.g. two Rachio controllers). The helper should
    still return that integration once."""
    result = detect_irrigation_integrations(installed_domains=["rachio", "rachio", "rachio"])
    assert result == [("rachio", "Rachio (official)")]


# ─── custom mapping (for extensibility) ──────────────────────────────


def test_custom_known_mapping_overrides_default():
    """The helper accepts a custom known-domains mapping so the project
    can ship a v1.1 mapping JSON without changing the call sites."""
    result = detect_irrigation_integrations(
        installed_domains=["foo_irrigation", "bar"],
        known_domains={"foo_irrigation": "Foo Brand", "qux": "Qux Brand"},
    )
    assert result == [("foo_irrigation", "Foo Brand")]


def test_default_known_mapping_includes_expected_brands():
    """Smoke check: the shipped mapping has the brands we promised in PRD."""
    for required in (
        "rachio",
        "rachio_local",
        "hydrawise",
        "rainmachine",
        "bhyve",
        "opensprinkler",
    ):
        assert required in KNOWN_IRRIGATION_DOMAINS, f"missing brand: {required}"


# ─── input types ─────────────────────────────────────────────────────


def test_accepts_iterable_not_just_list():
    """`set` and generators should work too — the helper takes any iterable."""
    result_from_set = detect_irrigation_integrations(installed_domains={"rachio", "hydrawise"})
    result_from_gen = detect_irrigation_integrations(
        installed_domains=(d for d in ["rachio", "hydrawise"])
    )
    assert {d for d, _ in result_from_set} == {"rachio", "hydrawise"}
    assert {d for d, _ in result_from_gen} == {"rachio", "hydrawise"}


# ─── value structure ─────────────────────────────────────────────────


def test_returns_tuples_of_domain_and_display_name():
    result = detect_irrigation_integrations(installed_domains=["rachio"])
    domain, display = result[0]
    assert isinstance(domain, str)
    assert isinstance(display, str)
    assert display != domain  # display is human-friendly, not the technical id


# ════════════════════════════════════════════════════════════════════
# select_zone_candidates
# ════════════════════════════════════════════════════════════════════


def _entity(
    entity_id,
    domain="switch",
    config_entry_domain=None,
    name=None,
    original_name=None,
    disabled=False,
    **extra,
):
    """Build a small EntityDescriptor for tests."""
    return {
        "entity_id": entity_id,
        "domain": domain,
        "config_entry_domain": config_entry_domain,
        "name": name,
        "original_name": original_name,
        "disabled": disabled,
        **extra,
    }


def test_zone_candidates_only_returns_switches():
    """Non-switch entities (sensors, binary_sensors, etc.) are skipped."""
    entities = [
        _entity("switch.front_lawn", config_entry_domain="rachio", name="Front Lawn"),
        _entity(
            "sensor.front_lawn_battery",
            domain="sensor",
            config_entry_domain="rachio",
            name="Battery",
        ),
        _entity(
            "binary_sensor.rachio_online", domain="binary_sensor", config_entry_domain="rachio"
        ),
    ]
    result = select_zone_candidates(entities, controller_domain="rachio")
    assert [e["entity_id"] for e in result] == ["switch.front_lawn"]


def test_zone_candidates_filters_by_controller_domain_when_specified():
    """When auto-import mode, only switches from the chosen integration come back."""
    entities = [
        _entity("switch.front_lawn", config_entry_domain="rachio", name="Front Lawn"),
        _entity("switch.back_lawn", config_entry_domain="rachio", name="Back Lawn"),
        _entity("switch.garage_door", config_entry_domain="myq", name="Garage Door"),
        _entity("switch.living_lamp", config_entry_domain="tplink", name="Living Lamp"),
    ]
    result = select_zone_candidates(entities, controller_domain="rachio")
    domains = {e["entity_id"] for e in result}
    assert domains == {"switch.front_lawn", "switch.back_lawn"}


def test_zone_candidates_manual_mode_returns_all_switches():
    """When user picks 'manual', all switches are candidates regardless of source."""
    entities = [
        _entity("switch.front_lawn", config_entry_domain="rachio", name="Front Lawn"),
        _entity("switch.garage_pump", config_entry_domain="tasmota", name="Garage Pump"),
        _entity("sensor.foo", domain="sensor"),
    ]
    result = select_zone_candidates(entities, controller_domain=None)
    assert {e["entity_id"] for e in result} == {"switch.front_lawn", "switch.garage_pump"}


def test_zone_candidates_excludes_disabled_entities():
    """Disabled entities never appear in the picker."""
    entities = [
        _entity("switch.front_lawn", config_entry_domain="rachio", name="Front Lawn"),
        _entity("switch.unused_zone", config_entry_domain="rachio", name="Unused", disabled=True),
    ]
    result = select_zone_candidates(entities, controller_domain="rachio")
    assert [e["entity_id"] for e in result] == ["switch.front_lawn"]


def test_zone_candidates_excludes_master_valve_and_schedule_switches():
    """Heuristic: 'master_valve' and 'schedule' substrings aren't real zones."""
    entities = [
        _entity("switch.front_lawn", config_entry_domain="rachio", name="Front Lawn"),
        _entity("switch.master_valve", config_entry_domain="rachio", name="Master valve"),
        _entity(
            "switch.morning_schedule", config_entry_domain="rachio_local", name="Morning Schedule"
        ),
        _entity("switch.rachio_standby", config_entry_domain="rachio", name="Standby"),
    ]
    result = select_zone_candidates(entities, controller_domain=None)
    assert [e["entity_id"] for e in result] == ["switch.front_lawn"]


def test_zone_candidates_does_not_exclude_zones_named_test():
    """Regression: 'test' in a zone name must NOT cause exclusion.

    A user smoke-testing or with a zone literally named 'Test Lawn'
    should still see it in the picker."""
    entities = [
        _entity("switch.test_lawn", config_entry_domain="template", name="Test Lawn"),
        _entity("switch.test_garden", config_entry_domain="template", name="Test Garden"),
    ]
    result = select_zone_candidates(entities, controller_domain=None)
    assert {e["entity_id"] for e in result} == {"switch.test_lawn", "switch.test_garden"}


def test_zone_candidates_sorted_alphabetically_by_display_name():
    """The picker list should appear sorted so users can find zones easily."""
    entities = [
        _entity("switch.zone_3", config_entry_domain="rachio", name="Side Yard"),
        _entity("switch.zone_1", config_entry_domain="rachio", name="Back Lawn"),
        _entity("switch.zone_2", config_entry_domain="rachio", name="Front Lawn"),
    ]
    result = select_zone_candidates(entities, controller_domain="rachio")
    names = [e["name"] for e in result]
    assert names == ["Back Lawn", "Front Lawn", "Side Yard"]


def test_zone_candidates_uses_entity_id_when_no_friendly_name():
    """Sorting falls back to entity_id if no name is set."""
    entities = [
        _entity("switch.zzz", config_entry_domain="rachio"),
        _entity("switch.aaa", config_entry_domain="rachio"),
    ]
    result = select_zone_candidates(entities, controller_domain="rachio")
    assert [e["entity_id"] for e in result] == ["switch.aaa", "switch.zzz"]


def test_zone_candidates_empty_input_returns_empty():
    assert select_zone_candidates([], controller_domain="rachio") == []
    assert select_zone_candidates([], controller_domain=None) == []
