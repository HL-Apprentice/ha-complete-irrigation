"""HA Complete Irrigation Integration.

Entry point. Real coordinator, services, and entity platforms are added
in later vertical-slice issues (see docs/ISSUES.md). This loader sets up
the config entry and registers the custom sidebar panel.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["calendar", "binary_sensor"]

PANEL_URL_PATH = "complete-irrigation"
PANEL_STATIC_URL = "/complete_irrigation_panel"
PANEL_JS_FILENAME = "complete-irrigation-panel.js"

# Key in `hass.data[DOMAIN]` reserved for our process-wide shared state.
# Config entry IDs use UUIDs, so there's no risk of collision with this.
_SHARED_KEY = "__shared__"


@dataclass
class _SharedState:
    """Process-wide state for this integration.

    Separate from per-config-entry data so we can clearly distinguish
    "things to clean up when ALL entries are gone" from "things to clean
    up when THIS entry is gone".
    """

    panel_registered: bool = False
    static_path_registered: bool = False


def _shared(hass: HomeAssistant) -> _SharedState:
    """Get or create the shared state for this integration domain."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if _SHARED_KEY not in domain_data:
        domain_data[_SHARED_KEY] = _SharedState()
    return domain_data[_SHARED_KEY]


def _entry_count(hass: HomeAssistant) -> int:
    """Return the number of live config entries for this domain."""
    return sum(1 for k in hass.data.get(DOMAIN, {}) if k != _SHARED_KEY)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Complete Irrigation from a config entry."""
    # Deferred imports — only loaded when HA actually calls us, so the
    # package stays importable in test environments without HA installed.
    from homeassistant.components.frontend import async_register_built_in_panel
    from homeassistant.components.http import StaticPathConfig

    from .coordinator import ScheduleCoordinator
    from .manual_run import ManualRunTracker
    from .services import _async_register_services

    _LOGGER.info("Setting up Complete Irrigation entry %s", entry.entry_id)

    shared = _shared(hass)

    # Per-entry coordinator drives the tick loop + persists schedules.
    coordinator = ScheduleCoordinator(hass, entry.entry_id)
    await coordinator.async_setup()

    hass.data[DOMAIN][entry.entry_id] = {
        "zones": entry.data.get("zones", []),
        "controller_domain": entry.data.get("controller_domain"),
        "manual_runs": ManualRunTracker(),
        "cancel_handles": {},
        "coordinator": coordinator,
    }

    # Register all integration services (idempotent — services are
    # shared across config entries via the _find_coordinator lookup).
    await _async_register_services(hass)

    # WebSocket API commands for the panel.
    from .ws_api import async_register_ws_commands

    async_register_ws_commands(hass)

    # Static path serving the panel JS — idempotent across multiple
    # config entries (HA doesn't expose an unregister API, so we
    # register at most once per HA session).
    if not shared.static_path_registered:
        frontend_dir = Path(__file__).parent / "frontend"
        await hass.http.async_register_static_paths(
            [
                StaticPathConfig(
                    PANEL_STATIC_URL,
                    str(frontend_dir),
                    cache_headers=False,
                )
            ]
        )
        shared.static_path_registered = True

    # Sidebar panel — also idempotent.
    if not shared.panel_registered:
        async_register_built_in_panel(
            hass,
            component_name="custom",
            sidebar_title="Irrigation",
            sidebar_icon="mdi:sprinkler-variant",
            frontend_url_path=PANEL_URL_PATH,
            config={
                "_panel_custom": {
                    "name": "complete-irrigation-panel",
                    # iframe sandbox protects HA's main frontend from
                    # any JS error in our panel code. See ADR/v0.2.0.
                    "embed_iframe": True,
                    "trust_external": False,
                    "js_url": f"{PANEL_STATIC_URL}/{PANEL_JS_FILENAME}",
                },
                "zones": entry.data.get("zones", []),
                "controller_domain": entry.data.get("controller_domain"),
            },
            require_admin=False,
        )
        shared.panel_registered = True

    if PLATFORMS:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry, cleaning up shared resources if it's the last one."""
    from homeassistant.components.frontend import async_remove_panel

    from .services import _async_unregister_services, _cleanup_entry_handles

    if PLATFORMS:
        unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        if not unload_ok:
            return False

    # Cancel any pending auto-stop timers / state listeners for this entry.
    entry_data = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if entry_data is not None:
        _cleanup_entry_handles(entry_data)
        coord = entry_data.get("coordinator")
        if coord is not None:
            coord.async_unload()

    # When the last config entry of ours unloads, also tear down the
    # shared panel registration and services. (Static paths can't be
    # unregistered in HA's API — they live until HA restart.)
    if _entry_count(hass) == 0:
        shared = _shared(hass)
        if shared.panel_registered:
            async_remove_panel(hass, PANEL_URL_PATH)
            shared.panel_registered = False
        _async_unregister_services(hass)

    return True
