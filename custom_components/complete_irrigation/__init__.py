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


async def _async_panel_version(hass: HomeAssistant) -> str:
    """Return the integration version for cache-busting the panel JS URL.

    v1.16.1 — was synchronously reading manifest.json on the event loop,
    which HA 2025+ flags as a blocking call. Now pulls from the
    already-loaded `Integration` object HA holds in its loader cache, so
    there's no file I/O at all.

    Falls back to "0" if the integration object isn't available so the
    panel still loads (worst case: no cache-bust).
    """
    try:
        from homeassistant.loader import async_get_integration

        integration = await async_get_integration(hass, DOMAIN)
        return integration.version or "0"
    except Exception:
        return "0"


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
    # v1.17 — guards against double-registering the action listener
    # when multiple config entries call _async_register_run_missed_action_listener.
    run_missed_listener_registered: bool = False


def _shared(hass: HomeAssistant) -> _SharedState:
    """Get or create the shared state for this integration domain."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if _SHARED_KEY not in domain_data:
        domain_data[_SHARED_KEY] = _SharedState()
    return domain_data[_SHARED_KEY]


def _entry_count(hass: HomeAssistant) -> int:
    """Return the number of live config entries for this domain."""
    return sum(1 for k in hass.data.get(DOMAIN, {}) if k != _SHARED_KEY)


def find_coordinator(hass: HomeAssistant):
    """Return the first live ScheduleCoordinator for this integration, or None.

    v1.15 — single source of truth. services.py + ws_api.py + ical_view.py
    all share this helper instead of each maintaining its own copy of the
    same _SHARED_KEY skip-and-walk loop.
    """
    for key, data in hass.data.get(DOMAIN, {}).items():
        if key == _SHARED_KEY:
            continue
        coord = data.get("coordinator")
        if coord is not None:
            return coord
    return None


def _async_register_run_missed_action_listener(hass: HomeAssistant, shared: _SharedState) -> None:
    """v1.17 — listen for the user tapping "Run now" on a missed-run
    notification (Companion app fires mobile_app_notification_action).

    The action's data payload carries zone_entity_id + minutes + a
    reference to the original SKIPPED History record, so the listener
    just calls run_zone and adds a triggers.recovered_from breadcrumb
    on the new run record.

    Idempotent: only one listener is registered per HA session even
    if multiple config entries set up.
    """
    if shared.run_missed_listener_registered:
        return
    shared.run_missed_listener_registered = True

    from .coordinator import RUN_MISSED_ACTION

    async def _on_action(event):
        action = event.data.get("action")
        if action != RUN_MISSED_ACTION:
            return  # also catches the _DISMISS sibling action (no-op)
        action_data = event.data.get("action_data") or {}
        entity_id = action_data.get("zone_entity_id")
        minutes = action_data.get("minutes")
        if not entity_id or not minutes:
            _LOGGER.warning("RUN_MISSED action without entity_id/minutes: %s", action_data)
            return
        _LOGGER.info(
            "Recovering missed run for %s via notification action (%s min)",
            entity_id,
            minutes,
        )
        # Stash the recovery breadcrumb so the history record for the
        # new run will reference the original missed record.
        coord = find_coordinator(hass)
        if coord is not None:
            entry_data = None
            for key, data in hass.data.get(DOMAIN, {}).items():
                if key == _SHARED_KEY:
                    continue
                if data.get("coordinator") is coord:
                    entry_data = data
                    break
            if entry_data is not None:
                meta = entry_data.setdefault("pending_run_meta", {})
                meta[entity_id] = {
                    "schedule_id": action_data.get("schedule_id"),
                    "schedule_name": action_data.get("schedule_name", "Recovered"),
                    "requested_minutes": int(minutes),
                    "triggers": {
                        "recovered_from": action_data.get("missed_record_id"),
                    },
                    "zone_name": None,  # services.py will fill from state attrs
                }
        await hass.services.async_call(
            DOMAIN,
            "run_zone",
            {"entity_id": entity_id, "minutes": int(minutes)},
            blocking=False,
        )

    hass.bus.async_listen("mobile_app_notification_action", _on_action)


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

    # PRD #13 — apply zone aliases the user set during config flow into
    # HA's entity registry, so panels/automations show the friendly names.
    aliases = entry.data.get("zone_aliases") or {}
    if aliases:
        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(hass)
        for entity_id, alias in aliases.items():
            try:
                registry.async_update_entity(entity_id, name=alias)
            except Exception:
                _LOGGER.warning("Could not rename %s to %r", entity_id, alias)

    # PRD #20 — seed plant category suggestions into per-zone moisture
    # config (only when the user hasn't already set a category).
    suggestions = entry.data.get("zone_category_suggestions") or {}
    if suggestions:
        zones_cfg = coordinator.config.setdefault("zones", {})
        dirty = False
        for entity_id, cat in suggestions.items():
            zcfg = zones_cfg.setdefault(entity_id, {})
            if not zcfg.get("category"):
                zcfg["category"] = cat
                dirty = True
        if dirty:
            await coordinator.async_save_config()

    # Register all integration services (idempotent — services are
    # shared across config entries via the _find_coordinator lookup).
    await _async_register_services(hass)

    # WebSocket API commands for the panel.
    from .ical_view import async_register_ical_view
    from .ws_api import async_register_ws_commands

    async_register_ws_commands(hass)
    async_register_ical_view(hass)

    # v1.17 — listen for "Run now" taps from missed-run notifications.
    # Idempotent: only registered once per HA session even if multiple
    # config entries set up.
    _async_register_run_missed_action_listener(hass, shared)

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
        panel_version = await _async_panel_version(hass)
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
                    # Append the integration version so each release gets a
                    # unique URL — browsers will fetch the new file instead
                    # of serving the stale cached one.
                    "js_url": f"{PANEL_STATIC_URL}/{PANEL_JS_FILENAME}?v={panel_version}",
                },
                "zones": entry.data.get("zones", []),
                "controller_domain": entry.data.get("controller_domain"),
            },
            # Admin-only (v1.15): the panel drives hardware (run/stop zones)
            # and edits the shared schedule store. Non-admin HA users should
            # not be able to start/stop irrigation or change schedules.
            require_admin=True,
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
