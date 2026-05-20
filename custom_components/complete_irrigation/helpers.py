"""Pure-logic helpers — no HA dependency, easy to unit test.

These functions take plain Python data in and return plain Python data
out. The HA-coupled adapter that calls them lives in `__init__.py` and
`config_flow.py`.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping

from .const import KNOWN_IRRIGATION_DOMAINS


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
