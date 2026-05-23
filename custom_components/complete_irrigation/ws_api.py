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
WS_TYPE_GET_CONFIG = f"{DOMAIN}/get_config"
WS_TYPE_GET_ACTIVE_RUNS = f"{DOMAIN}/get_active_runs"
WS_TYPE_LIST_RUN_HISTORY = f"{DOMAIN}/list_run_history"


def _find_coordinator(hass: HomeAssistant):
    """Thin re-export so existing call sites keep working; the canonical
    implementation lives in __init__.py (v1.15 dedupe)."""
    from . import find_coordinator

    return find_coordinator(hass)


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


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): WS_TYPE_GET_CONFIG})
@websocket_api.async_response
async def get_config(hass, connection, msg):
    """Return the coordinator's full config (weather + zones).

    Admin-only — config exposes notify_targets, all rain/wind/temperature
    sensor entity_ids, and per-zone moisture settings. Read-only HA
    users see only what's exposed on their dashboards."""
    coord = _find_coordinator(hass)
    if coord is None:
        connection.send_result(msg["id"], {})
        return
    # Also include current lockout state for the panel banner
    payload = dict(coord.config)
    payload["lockout_until"] = coord.lockout_until.isoformat() if coord.lockout_until else None
    connection.send_result(msg["id"], payload)


@websocket_api.websocket_command({vol.Required("type"): WS_TYPE_GET_ACTIVE_RUNS})
@websocket_api.async_response
async def get_active_runs(hass, connection, msg):
    """Return all currently-active manual runs across all config entries.

    Each entry: {entity_id, deadline, duration_minutes}. The panel uses
    this on connect (and after every run/stop) to hydrate countdowns —
    so a page reload mid-run, or a run started via Developer Tools,
    both see the auto-stop deadline.
    """
    from . import _SHARED_KEY

    runs = []
    for key, data in hass.data.get(DOMAIN, {}).items():
        if key == _SHARED_KEY:
            continue
        tracker = data.get("manual_runs")
        if tracker is None:
            continue
        for r in tracker.active():
            runs.append(
                {
                    "entity_id": r.entity_id,
                    "deadline": r.deadline().isoformat(),
                    "duration_minutes": int(r.duration.total_seconds() // 60),
                }
            )
    connection.send_result(msg["id"], {"runs": runs})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_TYPE_LIST_RUN_HISTORY,
        vol.Optional("limit"): vol.All(int, vol.Range(min=1, max=1000)),
    }
)
@websocket_api.async_response
async def list_run_history(hass, connection, msg):
    """Return run-history records (newest first). Admin-only.

    Optional `limit` caps the list size on the WS payload. Default
    returns everything in the store (already pruned to <=1000).
    """
    coord = _find_coordinator(hass)
    if coord is None:
        connection.send_result(msg["id"], {"records": []})
        return
    records = coord.run_history.to_serializable()
    limit = msg.get("limit")
    if limit:
        records = records[:limit]
    connection.send_result(msg["id"], {"records": records})


def async_register_ws_commands(hass: HomeAssistant) -> None:
    """Register all WS commands. Idempotent."""
    websocket_api.async_register_command(hass, list_schedules)
    websocket_api.async_register_command(hass, get_config)
    websocket_api.async_register_command(hass, get_active_runs)
    websocket_api.async_register_command(hass, list_run_history)
