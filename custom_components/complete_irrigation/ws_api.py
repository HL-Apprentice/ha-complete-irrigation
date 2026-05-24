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
WS_TYPE_LIST_PLANNED_RUNS = f"{DOMAIN}/list_planned_runs"


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


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_TYPE_LIST_PLANNED_RUNS,
        # Optional ISO timestamps; default = today through 7 days from now
        # in HA's local time. We accept strings and parse them here so the
        # panel can send `new Date().toISOString()` directly.
        vol.Optional("from_dt"): str,
        vol.Optional("until_dt"): str,
    }
)
@websocket_api.async_response
async def list_planned_runs(hass, connection, msg):
    """Return resolved PlannedRuns in the requested window.

    v1.16 — the panel used to re-implement calendar expansion in JS for
    the Today calendar + Zones strip, missing start_date / repeat_annually
    / configurable zone_buffer. This endpoint surfaces the server-side
    `next_runs() → resolve_conflicts()` output instead so the panel
    cannot drift.

    The "skipped: cascade-cap" entries are returned too so the panel can
    show why a run isn't going to fire.
    """
    from datetime import datetime, timedelta, timezone

    from homeassistant.util import dt as dt_util

    from .conflict_resolver import POLICY_DEFER_NEW, resolve_conflicts
    from .run_planner import next_runs

    coord = _find_coordinator(hass)
    if coord is None:
        connection.send_result(msg["id"], {"runs": []})
        return

    def _parse(s, default):
        # v1.17.4 — sibling bug to v1.17.1: panel sends ISO strings
        # like "2026-05-24T13:00:00.000Z" which fromisoformat parses
        # as UTC-aware. Passing that straight into next_runs() makes
        # `from_dt.tzinfo = UTC`, and the planner localizes
        # `start_time` to UTC instead of the user's local zone — same
        # 7-hour offset bug as v1.17.1 fixed in _tick. ALWAYS convert
        # to local here, regardless of input tz, so the planner's
        # `datetime.combine(date, start_time, tzinfo=local)` produces
        # the user-expected wall-clock fire times.
        if not s:
            return default
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return default
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)  # noqa: UP017 — keep py3.9 compat
        return dt_util.as_local(dt)

    now = dt_util.now()
    from_dt = _parse(msg.get("from_dt"), now)
    until_dt = _parse(msg.get("until_dt"), now + timedelta(days=7))

    raw = next_runs(
        coord.schedule_store.enabled_schedules(),
        from_dt=from_dt,
        until_dt=until_dt,
        zone_buffer_seconds=coord.config.get("zone_buffer_seconds"),
    )
    resolved = resolve_conflicts(raw, POLICY_DEFER_NEW)

    runs = [
        {
            "zone_entity_id": r.zone_entity_id,
            "start_at": r.start_at.isoformat(),
            "duration_minutes": int(r.duration_minutes),
            "schedule_id": r.schedule_id,
            "schedule_name": r.schedule_name,
            "reason": r.reason,
        }
        for r in resolved
    ]
    connection.send_result(msg["id"], {"runs": runs})


def async_register_ws_commands(hass: HomeAssistant) -> None:
    """Register all WS commands. Idempotent."""
    websocket_api.async_register_command(hass, list_schedules)
    websocket_api.async_register_command(hass, get_config)
    websocket_api.async_register_command(hass, get_active_runs)
    websocket_api.async_register_command(hass, list_run_history)
    websocket_api.async_register_command(hass, list_planned_runs)
