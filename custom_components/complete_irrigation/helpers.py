"""Pure-logic helpers — no HA dependency, easy to unit test.

These functions take plain Python data in and return plain Python data
out. The HA-coupled adapter that calls them lives in `__init__.py` and
`config_flow.py`.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .const import KNOWN_IRRIGATION_DOMAINS

# An entity descriptor for the pure-logic functions. Keys (all optional
# except entity_id and domain):
#   entity_id           e.g. "switch.front_lawn"
#   domain              e.g. "switch", "sensor"
#   config_entry_id     id of the HA config entry that created this entity
#   config_entry_domain domain of the integration that created it (e.g. "rachio")
#   name                user-friendly name (overrides original_name if set)
#   original_name       name from the source integration
#   device_id           parent device id
#   disabled            True if the entity is disabled in HA
EntityDescriptor = dict[str, Any]

# Heuristic substrings that suggest a switch is not really a zone
# (e.g., master valve, test entities, pause buttons, schedule switches).
_NON_ZONE_HINTS = ("master", "_test", "schedule", "pause", "rain_delay", "standby")


def detect_irrigation_integrations(
    installed_domains: Iterable[str],
    known_domains: Mapping[str, str] | None = None,
) -> list[tuple[str, str]]:
    """Return (domain, display_name) pairs for irrigation integrations
    that the user already has installed.

    Pure function. Caller responsibility:
      • `installed_domains` is the set of HA integration domains currently
        configured (e.g. from `hass.config_entries.async_domains()`).
      • `known_domains` defaults to the project's shipped catalog. Override
        only in tests or to plug in a v1.1 mapping JSON.

    Result is deduplicated and sorted alphabetically by domain for stable
    UI rendering.
    """
    catalog = known_domains if known_domains is not None else KNOWN_IRRIGATION_DOMAINS
    # `set()` handles deduplication if the caller passes duplicates.
    matched = {d for d in installed_domains if d in catalog}
    return sorted((d, catalog[d]) for d in matched)


def select_zone_candidates(
    entities: Iterable[EntityDescriptor],
    controller_domain: str | None = None,
) -> list[EntityDescriptor]:
    """Filter entities down to candidate irrigation zone switches.

    `controller_domain`:
      - When set (e.g. "rachio"), only returns switches whose
        `config_entry_domain` matches — this is the auto-import path
        when the user picked a known irrigation integration.
      - When None (manual mode), returns all switches regardless of
        source integration.

    Non-zone switches are filtered out using `_NON_ZONE_HINTS` (master
    valves, schedule toggles, etc.). Disabled entities are excluded.

    Result is sorted alphabetically by display name for stable UI.
    """
    results: list[EntityDescriptor] = []
    for e in entities:
        if e.get("domain") != "switch":
            continue
        if e.get("disabled"):
            continue
        if controller_domain is not None and e.get("config_entry_domain") != controller_domain:
            continue
        if _looks_like_non_zone(e):
            continue
        results.append(e)
    return sorted(results, key=lambda e: _display_name(e).lower())


def _looks_like_non_zone(e: EntityDescriptor) -> bool:
    """True if the entity name/id contains hints it's not really a zone."""
    haystack = " ".join(
        str(e.get(k, "")).lower()
        for k in ("entity_id", "name", "original_name")
    )
    return any(hint in haystack for hint in _NON_ZONE_HINTS)


def _display_name(e: EntityDescriptor) -> str:
    """Best name to show in the UI for an entity descriptor."""
    return e.get("name") or e.get("original_name") or e.get("entity_id", "")
