"""HA Complete Irrigation Integration.

Entry point. Real coordinator, services, and entity platforms are added
in later vertical-slice issues (see docs/ISSUES.md). This loader sets up
the config entry and registers the custom sidebar panel.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = []

PANEL_URL_PATH = "complete-irrigation"
PANEL_STATIC_URL = "/complete_irrigation_panel"
PANEL_JS_FILENAME = "complete-irrigation-panel.js"


async def async_setup_entry(hass: "HomeAssistant", entry: "ConfigEntry") -> bool:
    """Set up Complete Irrigation from a config entry."""
    # Deferred imports — only loaded when HA actually calls us, so the
    # package stays importable in test environments without HA installed.
    from homeassistant.components.frontend import async_register_built_in_panel
    from homeassistant.components.http import StaticPathConfig

    _LOGGER.info("Setting up Complete Irrigation entry %s", entry.entry_id)
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "zones": entry.data.get("zones", []),
        "controller_domain": entry.data.get("controller_domain"),
    }

    # Register the static path serving the panel JS (idempotent across
    # multiple config entries since we use the same path).
    if not hass.data[DOMAIN].get("_static_path_registered"):
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
        hass.data[DOMAIN]["_static_path_registered"] = True

    # Register the sidebar panel.
    if not hass.data[DOMAIN].get("_panel_registered"):
        async_register_built_in_panel(
            hass,
            component_name="custom",
            sidebar_title="Irrigation",
            sidebar_icon="mdi:sprinkler-variant",
            frontend_url_path=PANEL_URL_PATH,
            config={
                "_panel_custom": {
                    "name": "complete-irrigation-panel",
                    "embed_iframe": False,
                    "trust_external": False,
                    # Plain script (not ES module) — safer for vanilla
                    # web components. Prevents poisoning HA's element
                    # registry if the panel JS fails mid-load.
                    "js_url": f"{PANEL_STATIC_URL}/{PANEL_JS_FILENAME}",
                },
                "zones": entry.data.get("zones", []),
                "controller_domain": entry.data.get("controller_domain"),
            },
            require_admin=False,
        )
        hass.data[DOMAIN]["_panel_registered"] = True

    if PLATFORMS:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: "HomeAssistant", entry: "ConfigEntry") -> bool:
    """Unload a config entry."""
    from homeassistant.components.frontend import async_remove_panel

    if PLATFORMS:
        unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        if not unload_ok:
            return False

    # Remove the panel and clean up — but only when this is the last
    # config entry of ours being unloaded.
    domain_data = hass.data.get(DOMAIN, {})
    domain_data.pop(entry.entry_id, None)
    other_entries = [k for k in domain_data.keys() if not k.startswith("_")]
    if not other_entries:
        if domain_data.get("_panel_registered"):
            async_remove_panel(hass, PANEL_URL_PATH)
            domain_data["_panel_registered"] = False
        # Static path is kept registered — HA tears it down on shutdown.

    return True
