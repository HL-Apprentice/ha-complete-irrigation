"""WebSocket API commands for the custom panel.

Just `complete_irrigation/list_schedules` for now — the panel calls
this after every mutation (add / update / delete / toggle) to refresh
its view of the schedule store.

If we add subscriptions later (so the panel auto-updates without polling),
that goes here too.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant.components import websocket_api

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

WS_TYPE_LIST_SCHEDULES = f"{DOMAIN}/list_schedules"


def _find_coordinator(hass: HomeAssistant):
    """Mirror of services._find_coordinator — duplicated to avoid an
    import cycle (services.py imports from here too in the future)."""
    from . import _SHARED_KEY

    for key, data in hass.data.get(DOMAIN, {}).items():
        if key == _SHARED_KEY:
            continue
        coord = data.get("coordinator")
        if coord is not None:
            return coord
    return None


@websocket_api.websocket_command({vol.Required("type"): WS_TYPE_LIST_SCHEDULES})
@websocket_api.async_response
async def list_schedules(hass, connection, msg):
    """Return all schedules (serialized form) to the panel."""
    coord = _find_coordinator(hass)
    if coord is None:
        connection.send_result(msg["id"], {"schedules": []})
        return
    connection.send_result(
        msg["id"],
        {"schedules": coord.schedule_store.to_serializable()},
    )


def async_register_ws_commands(hass: HomeAssistant) -> None:
    """Register all WS commands. Idempotent."""
    websocket_api.async_register_command(hass, list_schedules)
