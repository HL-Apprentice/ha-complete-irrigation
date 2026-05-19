"""HA Complete Irrigation Integration.

Entry point. Real coordinator, services, and entity platforms are added in
later vertical-slice issues (see docs/ISSUES.md). This stub is enough to make
the integration installable so we can iterate on top of it.
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Entity platforms come online as later slices land.
PLATFORMS: list[str] = []


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Complete Irrigation from a config entry."""
    _LOGGER.info("Setting up Complete Irrigation entry %s", entry.entry_id)
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        # Placeholder for runtime state populated by later slices:
        #   "coordinator": <DataUpdateCoordinator>
        #   "schedules": <ScheduleStore>
        #   "engine":    <DecisionEngine>
    }

    if PLATFORMS:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if PLATFORMS:
        unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        if not unload_ok:
            return False

    hass.data[DOMAIN].pop(entry.entry_id, None)
    return True
