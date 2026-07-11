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
from .daily_planner import serialize_daily_plan
from .hydraulics import DEFAULT_EFFICIENCY
from .plant_design import build_yard_report, serialize_loop_report

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

WS_TYPE_LIST_SCHEDULES = f"{DOMAIN}/list_schedules"
WS_TYPE_GET_CONFIG = f"{DOMAIN}/get_config"
WS_TYPE_GET_ACTIVE_RUNS = f"{DOMAIN}/get_active_runs"
WS_TYPE_LIST_RUN_HISTORY = f"{DOMAIN}/list_run_history"
WS_TYPE_LIST_PLANNED_RUNS = f"{DOMAIN}/list_planned_runs"
WS_TYPE_LIST_PLANTS = f"{DOMAIN}/list_plants"
WS_TYPE_YARD_REPORT = f"{DOMAIN}/yard_report"
WS_TYPE_GET_DAILY_PLAN = f"{DOMAIN}/get_daily_plan"
WS_TYPE_LIST_CARE_TASKS = f"{DOMAIN}/list_care_tasks"  # v1.35
WS_TYPE_WATERING_DIAGNOSIS = f"{DOMAIN}/watering_diagnosis"  # v1.35


def _find_coordinator(hass: HomeAssistant):
    """Thin re-export so existing call sites keep working; the canonical
    implementation lives in __init__.py (v1.15 dedupe)."""
    from . import find_coordinator

    return find_coordinator(hass)


# v1.17.10 — admin-only. The panel is admin-only (v1.15.0 S3) but the
# WS commands behind it were callable by any authenticated HA user.
# Closes the consistency gap so non-admin / read-only users can't
# enumerate schedules via Developer Tools or a custom dashboard card.
@websocket_api.require_admin
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


# v1.17.10 — admin-only (see comment above list_schedules).
@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): WS_TYPE_GET_ACTIVE_RUNS})
@websocket_api.async_response
async def get_active_runs(hass, connection, msg):
    """Return all currently-active manual runs across all config entries.

    Each entry: {entity_id, deadline, duration_minutes}. The panel uses
    this on connect (and after every run/stop) to hydrate countdowns —
    so a page reload mid-run, or a run started via Developer Tools,
    both see the auto-stop deadline.

    v1.30 — ALSO returns `sessions`: zones with an active run SESSION and the whole
    run's deadline. A chunked run is one session spanning all its blocks + the 30s
    inter-block gaps; the per-block manual run (in `runs`) clears between blocks, so
    `sessions` is what tells the Today card the zone is still running through a gap —
    WITHOUT falsely showing a rain/moisture/wind-GATED run as running (a gated run
    never creates a session). `runs` still drives the per-block countdown.
    """
    from . import _SHARED_KEY

    runs = []
    sessions_out = []
    for key, data in hass.data.get(DOMAIN, {}).items():
        if key == _SHARED_KEY:
            continue
        tracker = data.get("manual_runs")
        if tracker is not None:
            for r in tracker.active():
                runs.append(
                    {
                        "entity_id": r.entity_id,
                        "deadline": r.deadline().isoformat(),
                        "duration_minutes": int(r.duration.total_seconds() // 60),
                    }
                )
        coord = data.get("coordinator")
        sessions = (coord.config.get("active_run_sessions") if coord else None) or {}
        for zone, sess in sessions.items():
            # Skip a session that's GATED (waiting on an unavailable switch during a
            # restart resume) — the valve is off, so reporting it would show a false
            # "Running" on the card until the retry fires.
            if isinstance(sess, dict) and sess.get("deadline") and not sess.get("resuming_gated"):
                sessions_out.append({"entity_id": zone, "deadline": sess["deadline"]})
    connection.send_result(msg["id"], {"runs": runs, "sessions": sessions_out})


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


# v1.17.10 — admin-only. Added in v1.16.0 (after the v1.15.0 review)
# and missed the require_admin guard the sibling commands got. Closes
# that regression.
@websocket_api.require_admin
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
        sun_times=coord.sun_times,
    )
    resolved = resolve_conflicts(raw, POLICY_DEFER_NEW, independent_zones=coord.independent_zones)

    # v1.18 — attach each schedule's color so the day calendar can tint
    # pills. Build a {schedule_id: color} map once rather than per-run.
    color_by_id = {s.id: s.color for s in coord.schedule_store.all()}

    runs = [
        {
            "zone_entity_id": r.zone_entity_id,
            "start_at": r.start_at.isoformat(),
            "duration_minutes": int(r.duration_minutes),
            "schedule_id": r.schedule_id,
            "schedule_name": r.schedule_name,
            "reason": r.reason,
            "color": color_by_id.get(r.schedule_id),
        }
        for r in resolved
    ]
    connection.send_result(msg["id"], {"runs": runs})


# ── v2: plant-aware irrigation ──────────────────────────────────────


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): WS_TYPE_LIST_PLANTS})
@websocket_api.async_response
async def list_plants(hass, connection, msg):
    """Return all plants (serialized) for the panel's plant editor. v1.35 adds
    the active light-survey sessions (plant_id -> progress) so the panel can
    show a live 'surveying…' state without a new round-trip."""
    coord = _find_coordinator(hass)
    if coord is None:
        connection.send_result(msg["id"], {"plants": [], "active_light_surveys": {}})
        return
    connection.send_result(
        msg["id"],
        {
            "plants": coord.plants.to_serializable(),
            "active_light_surveys": coord.light_survey_status(),
        },
    )


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): WS_TYPE_LIST_CARE_TASKS})
@websocket_api.async_response
async def list_care_tasks(hass, connection, msg):
    """v1.35 — care-task reminders with computed due state for the panel."""
    from homeassistant.util import dt as dt_util

    coord = _find_coordinator(hass)
    if coord is None:
        connection.send_result(msg["id"], {"tasks": []})
        return
    now_ts = int(dt_util.now().timestamp())
    tasks = []
    for t in coord.care_tasks.all():
        d = t.to_dict()
        d["next_due_ts"] = t.next_due_ts()
        d["is_due"] = t.is_due(now_ts)
        d["display_name"] = t.display_name()
        tasks.append(d)
    connection.send_result(msg["id"], {"tasks": tasks})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_TYPE_WATERING_DIAGNOSIS,
        vol.Required("zone_entity_id"): str,
    }
)
@websocket_api.async_response
async def watering_diagnosis(hass, connection, msg):
    """v1.35 — advisory over/under-watering diagnosis card for one zone."""
    coord = _find_coordinator(hass)
    if coord is None:
        connection.send_result(msg["id"], {"diagnosis": None})
        return
    card = coord.diagnose_zone_watering(msg["zone_entity_id"])
    connection.send_result(msg["id"], {"diagnosis": card.to_dict()})


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): WS_TYPE_YARD_REPORT})
@websocket_api.async_response
async def yard_report(hass, connection, msg):
    """Per-loop water design report: each plant's need + recommended emitters
    + OK/UNDER/OVER and design/capacity warnings, computed from the plants,
    their zones' schedules, and the configured reference ET."""
    coord = _find_coordinator(hass)
    if coord is None:
        connection.send_result(msg["id"], {"reports": [], "eto_in_week": None})
        return
    # v1.28 — effective_eto() is auto (FAO-56 from weather) when enabled+fresh,
    # else the manual figure; eto_status() carries the source for the UI.
    status = coord.eto_status()
    eto = float(status["eto_in_week"])
    eff = float(coord.config.get("drip_efficiency", DEFAULT_EFFICIENCY))
    reports = build_yard_report(coord.plants.all(), coord.schedule_store.all(), eto, eff)
    connection.send_result(
        msg["id"],
        {
            "reports": [serialize_loop_report(r) for r in reports],
            "drip_efficiency": eff,
            "yard_map": coord.config.get("yard_map"),  # v1.30 — aerial backdrop config
            **status,
        },
    )


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): WS_TYPE_GET_DAILY_PLAN})
@websocket_api.async_response
async def get_daily_plan(hass, connection, msg):
    """The advisory daily plan (today's runs prioritized by urgency, with skip hints
    for saturated zones) for the panel's 'Today's Plan' card. Advisory only."""
    coord = _find_coordinator(hass)
    if coord is None:
        connection.send_result(msg["id"], {"summary": "No runs planned today.", "items": []})
        return
    connection.send_result(msg["id"], serialize_daily_plan(coord.build_today_plan()))


def async_register_ws_commands(hass: HomeAssistant) -> None:
    """Register all WS commands. Idempotent."""
    websocket_api.async_register_command(hass, list_schedules)
    websocket_api.async_register_command(hass, get_config)
    websocket_api.async_register_command(hass, get_active_runs)
    websocket_api.async_register_command(hass, list_run_history)
    websocket_api.async_register_command(hass, list_planned_runs)
    websocket_api.async_register_command(hass, list_plants)
    websocket_api.async_register_command(hass, yard_report)
    websocket_api.async_register_command(hass, get_daily_plan)
    websocket_api.async_register_command(hass, list_care_tasks)
    websocket_api.async_register_command(hass, watering_diagnosis)
