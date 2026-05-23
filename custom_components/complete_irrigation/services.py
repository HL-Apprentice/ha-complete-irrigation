"""HA service registration: manual zone runs + schedule CRUD.

Run-on-demand services:
  • complete_irrigation.run_zone(entity_id, minutes=10)
  • complete_irrigation.stop_zone(entity_id)

Schedule CRUD services (operate against the ScheduleCoordinator):
  • complete_irrigation.add_schedule(name, zone_entity_id, start_time,
      duration_minutes, weekdays, enabled?)
  • complete_irrigation.update_schedule(schedule_id, name?, zone_entity_id?,
      start_time?, duration_minutes?, weekdays?, enabled?)
  • complete_irrigation.delete_schedule(schedule_id)
  • complete_irrigation.set_schedule_enabled(schedule_id, enabled)

All schedule CRUD operations persist immediately via coordinator.async_save().
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.const import SERVICE_TURN_OFF, SERVICE_TURN_ON
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.util import dt as dt_util

from .const import DEFAULT_MANUAL_RUN_MINUTES, DOMAIN, MAX_MANUAL_RUN_MINUTES
from .manual_run import ManualRun, validate_run_duration
from .run_history import SOURCE_MANUAL, SOURCE_SCHEDULED
from .schedule import Schedule, ZoneStep

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall

_LOGGER = logging.getLogger(__name__)

SERVICE_RUN_ZONE = "run_zone"
SERVICE_STOP_ZONE = "stop_zone"
SERVICE_CLEAR_RUN_HISTORY = "clear_run_history"
SERVICE_ADD_SCHEDULE = "add_schedule"
SERVICE_UPDATE_SCHEDULE = "update_schedule"
SERVICE_DELETE_SCHEDULE = "delete_schedule"
SERVICE_SET_SCHEDULE_ENABLED = "set_schedule_enabled"
SERVICE_SET_WEATHER_CONFIG = "set_weather_config"
SERVICE_SET_ZONE_MOISTURE = "set_zone_moisture"
SERVICE_CLEAR_RAIN_LOCKOUT = "clear_rain_lockout"
SERVICE_SET_NOTIFICATION_CONFIG = "set_notification_config"
SERVICE_TEST_NOTIFICATION = "test_notification"
SERVICE_START_ESTABLISHMENT = "start_establishment"

MAX_SCHEDULE_DURATION_MIN = 480  # 8 hours — safety cap for scheduled runs

# Voluptuous schema for validation at the HA boundary.
_RUN_ZONE_SCHEMA = vol.Schema(
    {
        vol.Required("entity_id"): cv.entity_id,
        vol.Optional("minutes", default=DEFAULT_MANUAL_RUN_MINUTES): vol.All(
            vol.Coerce(float),
            vol.Range(min=1, max=MAX_MANUAL_RUN_MINUTES),
        ),
    }
)

_STOP_ZONE_SCHEMA = vol.Schema(
    {
        vol.Required("entity_id"): cv.entity_id,
    }
)

_WEEKDAYS_SCHEMA = vol.All(
    cv.ensure_list,
    [vol.All(vol.Coerce(int), vol.Range(min=0, max=6))],
    vol.Length(min=1),
)

_ZONE_STEP_SCHEMA = vol.Schema(
    {
        vol.Required("zone_entity_id"): cv.entity_id,
        vol.Required("duration_minutes"): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=MAX_SCHEDULE_DURATION_MIN)
        ),
    }
)


def _no_control_chars(value: str) -> str:
    """v1.15 — reject control characters in user strings.

    Schedule names land in the iCal feed and the run_history records,
    both of which are echoed verbatim to other surfaces (calendar
    subscribers, panel). An embedded CR/LF would let a name carry
    extra ICS fields or break log parsing."""
    for c in value:
        if c < " " and c not in ("\t",):
            raise vol.Invalid("control characters not allowed in name")
    return value


def _validate_notify_target(value: str) -> str:
    """v1.15 — restrict notify targets to the `notify.*` service domain.

    Without this guard, an attacker who can call set_notification_config
    could install something like `shell_command.curl_exfil` as a "notify
    target" and have it invoked on every notification event. We only
    allow the `notify` domain since that's the only one whose service
    contract takes (title, message)."""
    if not isinstance(value, str) or not value.strip():
        raise vol.Invalid("notify target must be a non-empty string")
    parts = value.strip().split(".", 1)
    if len(parts) != 2 or parts[0] != "notify" or not parts[1]:
        raise vol.Invalid(f"notify target must be of the form 'notify.<service>', got {value!r}")
    return value.strip()


_ADD_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Required("name"): vol.All(cv.string, vol.Length(min=1, max=80), _no_control_chars),
        vol.Required("zone_entity_id"): cv.entity_id,
        vol.Required("start_time"): cv.time,
        vol.Required("duration_minutes"): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=MAX_SCHEDULE_DURATION_MIN)
        ),
        # weekdays is required only when mode=weekdays. Schema accepts
        # either; mode-specific validation happens at the Schedule() ctor.
        vol.Optional("weekdays", default=[]): vol.All(
            cv.ensure_list,
            [vol.All(vol.Coerce(int), vol.Range(min=0, max=6))],
        ),
        vol.Optional("enabled", default=True): cv.boolean,
        vol.Optional("mode", default="weekdays"): vol.In(
            ("weekdays", "interval", "interval_hours")
        ),
        vol.Optional("interval_days"): vol.All(vol.Coerce(int), vol.Range(min=1, max=365)),
        vol.Optional("interval_hours"): vol.All(vol.Coerce(int), vol.Range(min=1, max=72)),
        vol.Optional("interval_anchor"): cv.date,
        vol.Optional("interval_end_time"): vol.Any(None, cv.time),
        # Active period (v1.12). end_date already accepted (no field here —
        # historically Schedule.end_date wasn't a service field; we add it
        # below alongside start_date + repeat_annually).
        vol.Optional("start_date"): vol.Any(None, cv.date),
        vol.Optional("end_date"): vol.Any(None, cv.date),
        vol.Optional("repeat_annually", default=False): cv.boolean,
        # Multi-zone: optional list of {zone_entity_id, duration_minutes}.
        # First step must equal zone_entity_id + duration_minutes.
        vol.Optional("zone_steps", default=[]): vol.All(
            cv.ensure_list,
            [_ZONE_STEP_SCHEMA],
        ),
    }
)

_UPDATE_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Required("schedule_id"): cv.string,
        vol.Optional("name"): vol.All(cv.string, vol.Length(min=1, max=80), _no_control_chars),
        vol.Optional("zone_entity_id"): cv.entity_id,
        vol.Optional("start_time"): cv.time,
        vol.Optional("duration_minutes"): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=MAX_SCHEDULE_DURATION_MIN)
        ),
        vol.Optional("weekdays"): _WEEKDAYS_SCHEMA,
        vol.Optional("enabled"): cv.boolean,
        vol.Optional("mode"): vol.In(("weekdays", "interval", "interval_hours")),
        vol.Optional("interval_days"): vol.All(vol.Coerce(int), vol.Range(min=1, max=365)),
        vol.Optional("interval_hours"): vol.All(vol.Coerce(int), vol.Range(min=1, max=72)),
        vol.Optional("interval_anchor"): cv.date,
        vol.Optional("interval_end_time"): vol.Any(None, cv.time),
        vol.Optional("start_date"): vol.Any(None, cv.date),
        vol.Optional("end_date"): vol.Any(None, cv.date),
        vol.Optional("repeat_annually"): cv.boolean,
        vol.Optional("zone_steps"): vol.All(cv.ensure_list, [_ZONE_STEP_SCHEMA]),
    }
)

_DELETE_SCHEDULE_SCHEMA = vol.Schema({vol.Required("schedule_id"): cv.string})

_SET_SCHEDULE_ENABLED_SCHEMA = vol.Schema(
    {
        vol.Required("schedule_id"): cv.string,
        vol.Required("enabled"): cv.boolean,
    }
)

_SET_WEATHER_CONFIG_SCHEMA = vol.Schema(
    {
        # Either singular (legacy) or plural list of rain sensors; the
        # first plural entry is treated as primary for lockout logic.
        vol.Optional("rain_sensor"): cv.entity_id,
        vol.Optional("rain_sensors"): vol.All(cv.ensure_list, [cv.entity_id]),
        vol.Optional("temperature_sensor"): cv.entity_id,
        vol.Optional("hot_threshold_f"): vol.All(vol.Coerce(float), vol.Range(min=50, max=130)),
        vol.Optional("boost_percent"): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
        # PRD #52 — wind-based defer. 0 (default) disables; otherwise
        # current wind ≥ threshold causes scheduled runs to skip.
        vol.Optional("wind_sensor"): cv.entity_id,
        vol.Optional("wind_defer_mph"): vol.All(vol.Coerce(float), vol.Range(min=0, max=80)),
    }
)

_SET_ZONE_MOISTURE_SCHEMA = vol.Schema(
    {
        vol.Required("zone_entity_id"): cv.entity_id,
        vol.Required("moisture_entities"): vol.All(cv.ensure_list, [cv.entity_id]),
        vol.Optional("combine_mode", default="average"): vol.In(
            ["average", "lowest", "highest", "primary"]
        ),
        vol.Optional("min_pct", default=21): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
        vol.Optional("target_pct", default=31): vol.All(
            vol.Coerce(float), vol.Range(min=0, max=100)
        ),
        vol.Optional("max_pct", default=40): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
        # Aux climate sensors for the zone — display-only on the Zones
        # tab. Both are averaged when multiple sensors are bound.
        vol.Optional("temperature_entities"): vol.All(cv.ensure_list, [cv.entity_id]),
        vol.Optional("humidity_entities"): vol.All(cv.ensure_list, [cv.entity_id]),
        vol.Optional("category"): cv.string,
    }
)

_CLEAR_RAIN_LOCKOUT_SCHEMA = vol.Schema({})

_SET_NOTIFICATION_CONFIG_SCHEMA = vol.Schema(
    {
        # Single legacy target — still accepted for back-compat.
        vol.Optional("notify_target"): vol.Any("", _validate_notify_target),
        # Multi-target list. Accepted as list-of-strings OR a
        # comma/newline-separated string (the dispatcher normalizes).
        # When a string, each comma/newline-separated entry is validated
        # by the dispatcher; we accept the raw string here.
        vol.Optional("notify_targets"): vol.Any(cv.string, [_validate_notify_target]),
        vol.Optional("quiet_hours_start"): cv.string,
        vol.Optional("quiet_hours_end"): cv.string,
        vol.Optional("enabled"): cv.boolean,
        # Daily summary fires at quiet-hours-end if any zone is below its
        # configured min%. Defaults to true so it just works after setup.
        vol.Optional("low_moisture_alerts"): cv.boolean,
    }
)

_TEST_NOTIFICATION_SCHEMA = vol.Schema(
    {vol.Optional("message", default="Test"): vol.All(cv.string, vol.Length(max=1024))}
)

_SET_GENERAL_CONFIG_SCHEMA = vol.Schema(
    {
        # PRD #38 — inter-zone valve-settle buffer for multi-zone schedules.
        vol.Optional("zone_buffer_seconds"): vol.All(vol.Coerce(int), vol.Range(min=0, max=600)),
        # PRD #81 — snooze the Sunday weekly reminder until this date.
        vol.Optional("weekly_reminder_snoozed_until"): vol.Any(None, cv.date),
        # User-defined zone ordering for the panel (Today + Zones tabs).
        # When set, the panel renders zones in this order; zones added
        # later via the config flow are appended below this list.
        vol.Optional("zone_order"): vol.All(cv.ensure_list, [cv.entity_id]),
    }
)

_SET_CONFLICT_POLICY_SCHEMA = vol.Schema(
    {
        vol.Required("policy"): vol.In(["defer_new", "shift_existing", "split_difference"]),
    }
)

_START_ESTABLISHMENT_SCHEMA = vol.Schema(
    {
        vol.Required("zone_entity_id"): cv.entity_id,
        vol.Optional("cycles_per_day", default=3): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=8)
        ),
        vol.Optional("minutes_per_cycle", default=10): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=60)
        ),
        vol.Optional("days", default=12): vol.All(vol.Coerce(int), vol.Range(min=1, max=60)),
        vol.Optional("start_hour", default=6): vol.All(vol.Coerce(int), vol.Range(min=0, max=23)),
    }
)


def _find_coordinator(hass: HomeAssistant):
    """Thin re-export so existing call sites keep working; the canonical
    implementation lives in __init__.py (v1.15 dedupe)."""
    from . import find_coordinator

    return find_coordinator(hass)


def _find_entry_data(hass: HomeAssistant, entity_id: str) -> dict[str, Any] | None:
    """Find the config-entry data dict that owns this entity_id."""
    from . import _SHARED_KEY  # local import — avoid module-load coupling

    for key, data in hass.data.get(DOMAIN, {}).items():
        if key == _SHARED_KEY:
            continue
        if entity_id in data.get("zones", []):
            return data
    return None


def _cancel_handles(entry_data: dict[str, Any], entity_id: str) -> None:
    """Cancel and remove any pending timer + state listener for this entity."""
    handles = entry_data.setdefault("cancel_handles", {})
    pair = handles.pop(entity_id, None)
    if pair is None:
        return
    cancel_timer, cancel_listener = pair
    if cancel_timer:
        cancel_timer()
    if cancel_listener:
        cancel_listener()


async def _async_register_services(hass: HomeAssistant) -> None:
    """Register both services. Idempotent — safe to call from multiple entries."""

    if hass.services.has_service(DOMAIN, SERVICE_RUN_ZONE):
        return  # already registered

    async def handle_run_zone(call: ServiceCall) -> None:
        data = _RUN_ZONE_SCHEMA(dict(call.data))
        entity_id: str = data["entity_id"]
        try:
            minutes = validate_run_duration(data["minutes"])
        except (TypeError, ValueError) as err:
            raise vol.Invalid(str(err)) from err

        entry_data = _find_entry_data(hass, entity_id)
        if entry_data is None:
            _LOGGER.warning(
                "run_zone called for %s, not a configured zone — ignoring",
                entity_id,
            )
            return

        # Replace any existing run for this entity (cancel old timer + listener).
        _cancel_handles(entry_data, entity_id)

        # Hard-failure guard (PRD #43): if the underlying switch entity is
        # missing or already unavailable, refuse to run and notify the user
        # immediately instead of silently logging.
        state = hass.states.get(entity_id)
        coord = entry_data.get("coordinator")
        if state is None or state.state == "unavailable":
            _LOGGER.warning(
                "run_zone: entity %s is %s — refusing to start",
                entity_id,
                "missing" if state is None else "unavailable",
            )
            if coord is not None and coord.notifier is not None:
                from .notifications import CATEGORY_CRITICAL

                friendly = (state.attributes.get("friendly_name") if state else None) or entity_id
                await coord.notifier.notify(
                    f"Cannot start {friendly} — the zone switch is "
                    f"{'missing from HA' if state is None else 'unavailable'}.",
                    title="Zone failed to start",
                    category=CATEGORY_CRITICAL,
                    event_type="zone_start_failed",
                )
            return

        # Record the run + turn the switch on.
        now = dt_util.utcnow()
        run = ManualRun(
            entity_id=entity_id,
            start_at=now,
            duration=timedelta(minutes=minutes),
        )
        entry_data["manual_runs"].start(run)

        # Pop any pending metadata stashed by the coordinator (scheduled
        # run) — if present, this is a scheduled call; otherwise manual.
        meta = entry_data.get("pending_run_meta", {}).pop(entity_id, None)
        source = SOURCE_SCHEDULED if meta else SOURCE_MANUAL

        try:
            await hass.services.async_call(
                "switch",
                SERVICE_TURN_ON,
                {"entity_id": entity_id},
                blocking=True,
            )
        except Exception as err:
            # Roll back tracker + notify so the user knows the start failed
            # even though we recorded a run.
            entry_data["manual_runs"].stop(entity_id)
            _LOGGER.exception("switch.turn_on failed for %s", entity_id)
            if coord is not None and coord.notifier is not None:
                from .notifications import CATEGORY_CRITICAL

                friendly = state.attributes.get("friendly_name") or entity_id
                await coord.notifier.notify(
                    f"Zone {friendly} failed to start: {err}",
                    title="Zone failed to start",
                    category=CATEGORY_CRITICAL,
                    event_type="zone_start_failed",
                )
            return

        # ── Run history: record the start ──
        # `requested_minutes` for scheduled runs uses the ORIGINAL planned
        # duration (before boost), so the actual_minutes vs requested
        # comparison reflects gate adjustments. Manual runs just use the
        # service call's minutes.
        if coord is not None:
            zone_name_attr = (state.attributes.get("friendly_name") if state else None) or entity_id
            coord.run_history.start_run(
                zone_entity_id=entity_id,
                zone_name=(meta or {}).get("zone_name") or zone_name_attr,
                requested_minutes=(meta or {}).get("requested_minutes", minutes),
                source=source,
                started_at=now,
                schedule_id=(meta or {}).get("schedule_id"),
                schedule_name=(meta or {}).get("schedule_name", ""),
                triggers=(meta or {}).get("triggers") or {},
            )
            hass.async_create_task(coord.async_save_run_history())

        # ── auto-stop timer ──
        async def _auto_stop(_now=None):
            run_now = entry_data["manual_runs"].get(entity_id)
            if run_now is None:
                return  # already cleaned up by an external off
            entry_data["manual_runs"].stop(entity_id)
            await hass.services.async_call(
                "switch",
                SERVICE_TURN_OFF,
                {"entity_id": entity_id},
                blocking=False,
            )
            _cancel_handles(entry_data, entity_id)
            # Run history: timer expired → completed
            if coord is not None:
                coord.run_history.complete_run(entity_id, ended_at=dt_util.utcnow())
                await coord.async_save_run_history()
            _LOGGER.info("Manual run for %s expired and was stopped", entity_id)

        cancel_timer = async_call_later(hass, minutes * 60, _auto_stop)

        # ── state listener ──
        # If the user turns the zone off some other way, cancel our auto-stop
        # so we don't double-trigger turn_off (and so we clear our run state).
        @callback
        def _on_state_change(event):
            new_state = event.data.get("new_state")
            if new_state is None or new_state.state == "on":
                return
            # Zone is off (or unavailable). Drop our run.
            if entry_data["manual_runs"].get(entity_id):
                entry_data["manual_runs"].stop(entity_id)
                _cancel_handles(entry_data, entity_id)
                # External off — treat as abort with a generic reason.
                if coord is not None:
                    coord.run_history.abort_run(
                        entity_id,
                        ended_at=dt_util.utcnow(),
                        reason="switch turned off externally",
                    )
                    hass.async_create_task(coord.async_save_run_history())
                _LOGGER.info(
                    "Manual run for %s ended externally — cancelled auto-stop",
                    entity_id,
                )

        cancel_listener = async_track_state_change_event(hass, [entity_id], _on_state_change)

        entry_data.setdefault("cancel_handles", {})[entity_id] = (
            cancel_timer,
            cancel_listener,
        )

        _LOGGER.info(
            "Started manual run: %s for %d minutes (deadline %s)",
            entity_id,
            minutes,
            run.deadline().isoformat(),
        )

    async def handle_stop_zone(call: ServiceCall) -> None:
        data = _STOP_ZONE_SCHEMA(dict(call.data))
        entity_id: str = data["entity_id"]

        entry_data = _find_entry_data(hass, entity_id)
        if entry_data is None:
            _LOGGER.warning(
                "stop_zone called for %s, not a configured zone — ignoring",
                entity_id,
            )
            return

        entry_data["manual_runs"].stop(entity_id)
        _cancel_handles(entry_data, entity_id)
        await hass.services.async_call(
            "switch",
            SERVICE_TURN_OFF,
            {"entity_id": entity_id},
            blocking=False,
        )
        # Run history: explicit Stop button → abort with reason.
        coord = entry_data.get("coordinator")
        if coord is not None:
            coord.run_history.abort_run(
                entity_id,
                ended_at=dt_util.utcnow(),
                reason="user pressed Stop",
            )
            await coord.async_save_run_history()
        _LOGGER.info("Stopped manual run for %s by request", entity_id)

    # ── Schedule CRUD ──────────────────────────────────────────────

    async def handle_add_schedule(call: ServiceCall) -> None:
        data = _ADD_SCHEDULE_SCHEMA(dict(call.data))
        coord = _find_coordinator(hass)
        if coord is None:
            _LOGGER.warning("add_schedule called but no coordinator available")
            return
        steps_raw = data.get("zone_steps") or []
        zone_steps = tuple(
            ZoneStep(
                zone_entity_id=s["zone_entity_id"],
                duration_minutes=int(s["duration_minutes"]),
            )
            for s in steps_raw
        )
        schedule = Schedule(
            id=uuid.uuid4().hex[:12],
            name=data["name"],
            zone_entity_id=data["zone_entity_id"],
            start_time=data["start_time"],
            duration_minutes=data["duration_minutes"],
            weekdays=tuple(sorted(data.get("weekdays") or ())),
            enabled=data["enabled"],
            mode=data["mode"],
            interval_days=data.get("interval_days"),
            interval_hours=data.get("interval_hours"),
            interval_anchor=data.get("interval_anchor"),
            interval_end_time=data.get("interval_end_time"),
            start_date=data.get("start_date"),
            end_date=data.get("end_date"),
            repeat_annually=data.get("repeat_annually", False),
            zone_steps=zone_steps,
            # PRD #60 — provenance marker so calendar.py / establishment
            # can identify their own entries when #59 two-way calendar
            # edit lands. Anything coming through the service (panel,
            # automations, dev tools) gets "service".
            created_via="service",
        )
        coord.schedule_store.add(schedule)
        await coord.async_save()
        _LOGGER.info("Added schedule %s (%s) mode=%s", schedule.id, schedule.name, schedule.mode)

    async def handle_update_schedule(call: ServiceCall) -> None:
        data = _UPDATE_SCHEDULE_SCHEMA(dict(call.data))
        coord = _find_coordinator(hass)
        if coord is None:
            return
        existing = coord.schedule_store.get(data["schedule_id"])
        if existing is None:
            _LOGGER.warning("update_schedule: no such schedule %s", data["schedule_id"])
            return
        # v1.15 — collect only the fields the caller actually sent, then
        # let with_changes do the rest. Avoids the "added a field, forgot
        # to copy it in update_schedule" bug class.
        overrides: dict[str, Any] = {}
        for key in (
            "name",
            "zone_entity_id",
            "start_time",
            "duration_minutes",
            "enabled",
            "mode",
            "interval_days",
            "interval_hours",
            "interval_anchor",
            "interval_end_time",
            "start_date",
            "repeat_annually",
            "end_date",
        ):
            if key in data:
                overrides[key] = data[key]
        if "weekdays" in data:
            overrides["weekdays"] = tuple(sorted(data["weekdays"]))
        if "zone_steps" in data:
            overrides["zone_steps"] = tuple(
                ZoneStep(
                    zone_entity_id=s["zone_entity_id"],
                    duration_minutes=int(s["duration_minutes"]),
                )
                for s in (data["zone_steps"] or [])
            )
        merged = existing.with_changes(**overrides)
        coord.schedule_store.upsert(merged)
        await coord.async_save()
        _LOGGER.info("Updated schedule %s", merged.id)

    async def handle_delete_schedule(call: ServiceCall) -> None:
        data = _DELETE_SCHEDULE_SCHEMA(dict(call.data))
        coord = _find_coordinator(hass)
        if coord is None:
            return
        if coord.schedule_store.delete(data["schedule_id"]):
            await coord.async_save()
            _LOGGER.info("Deleted schedule %s", data["schedule_id"])

    async def handle_set_schedule_enabled(call: ServiceCall) -> None:
        data = _SET_SCHEDULE_ENABLED_SCHEMA(dict(call.data))
        coord = _find_coordinator(hass)
        if coord is None:
            return
        existing = coord.schedule_store.get(data["schedule_id"])
        if existing is None:
            return
        # v1.15 — with_changes preserves every field automatically, so
        # new Schedule attributes don't silently get dropped here.
        new = existing.with_changes(enabled=data["enabled"])
        coord.schedule_store.upsert(new)
        await coord.async_save()
        _LOGGER.info("Set schedule %s enabled=%s", existing.id, data["enabled"])

    hass.services.async_register(DOMAIN, SERVICE_RUN_ZONE, handle_run_zone, schema=_RUN_ZONE_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_STOP_ZONE, handle_stop_zone, schema=_STOP_ZONE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_ADD_SCHEDULE, handle_add_schedule, schema=_ADD_SCHEDULE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_UPDATE_SCHEDULE,
        handle_update_schedule,
        schema=_UPDATE_SCHEDULE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_DELETE_SCHEDULE,
        handle_delete_schedule,
        schema=_DELETE_SCHEDULE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_SCHEDULE_ENABLED,
        handle_set_schedule_enabled,
        schema=_SET_SCHEDULE_ENABLED_SCHEMA,
    )

    # ── Weather + per-zone moisture configuration ──────────────────

    async def handle_set_weather_config(call: ServiceCall) -> None:
        data = _SET_WEATHER_CONFIG_SCHEMA(dict(call.data))
        coord = _find_coordinator(hass)
        if coord is None:
            return
        for key in (
            "rain_sensor",
            "rain_sensors",
            "temperature_sensor",
            "hot_threshold_f",
            "boost_percent",
            "wind_sensor",
            "wind_defer_mph",
        ):
            if key in data:
                coord.config[key] = data[key]
        # If the user sets a non-empty rain_sensors list, mirror the first
        # one into rain_sensor so the existing lockout logic keeps working
        # without a coordinator change.
        rs_list = data.get("rain_sensors")
        if rs_list:
            coord.config["rain_sensor"] = rs_list[0]
        await coord.async_save_config()
        _LOGGER.info("Weather config updated: %s", data)

    async def handle_set_zone_moisture(call: ServiceCall) -> None:
        data = _SET_ZONE_MOISTURE_SCHEMA(dict(call.data))
        coord = _find_coordinator(hass)
        if coord is None:
            return
        zone_id = data.pop("zone_entity_id")
        coord.config.setdefault("zones", {})[zone_id] = {
            k: v for k, v in data.items() if v is not None
        }
        await coord.async_save_config()
        _LOGGER.info("Zone moisture config updated for %s: %s", zone_id, data)

    async def handle_clear_rain_lockout(call: ServiceCall) -> None:
        coord = _find_coordinator(hass)
        if coord is None:
            return
        coord.clear_lockout()
        _LOGGER.info("Rain lockout cleared by service call")

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_WEATHER_CONFIG,
        handle_set_weather_config,
        schema=_SET_WEATHER_CONFIG_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_ZONE_MOISTURE,
        handle_set_zone_moisture,
        schema=_SET_ZONE_MOISTURE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEAR_RAIN_LOCKOUT,
        handle_clear_rain_lockout,
        schema=_CLEAR_RAIN_LOCKOUT_SCHEMA,
    )

    async def handle_set_notification_config(call: ServiceCall) -> None:
        data = _SET_NOTIFICATION_CONFIG_SCHEMA(dict(call.data))
        coord = _find_coordinator(hass)
        if coord is None or coord.notifier is None:
            return
        coord.notifier.update_config(**data)
        coord.config.setdefault("notifications", {}).update(data)
        await coord.async_save_config()
        _LOGGER.info("Notification config updated: %s", data)

    async def handle_test_notification(call: ServiceCall) -> None:
        data = _TEST_NOTIFICATION_SCHEMA(dict(call.data))
        coord = _find_coordinator(hass)
        if coord is None or coord.notifier is None:
            return
        from .notifications import CATEGORY_CRITICAL

        await coord.notifier.notify(
            data["message"],
            title="Test notification",
            category=CATEGORY_CRITICAL,
            event_type="test",
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_NOTIFICATION_CONFIG,
        handle_set_notification_config,
        schema=_SET_NOTIFICATION_CONFIG_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_TEST_NOTIFICATION,
        handle_test_notification,
        schema=_TEST_NOTIFICATION_SCHEMA,
    )

    async def handle_set_conflict_policy(call: ServiceCall) -> None:
        data = _SET_CONFLICT_POLICY_SCHEMA(dict(call.data))
        coord = _find_coordinator(hass)
        if coord is None:
            return
        coord.config["conflict_policy"] = data["policy"]
        await coord.async_save_config()
        _LOGGER.info("Conflict policy updated: %s", data["policy"])

    hass.services.async_register(
        DOMAIN,
        "set_conflict_policy",
        handle_set_conflict_policy,
        schema=_SET_CONFLICT_POLICY_SCHEMA,
    )

    async def handle_set_general_config(call: ServiceCall) -> None:
        """PRD #38 + #81 — generic bucket for one-off global settings."""
        data = _SET_GENERAL_CONFIG_SCHEMA(dict(call.data))
        coord = _find_coordinator(hass)
        if coord is None:
            return
        if "zone_buffer_seconds" in data:
            coord.config["zone_buffer_seconds"] = data["zone_buffer_seconds"]
        if "weekly_reminder_snoozed_until" in data:
            v = data["weekly_reminder_snoozed_until"]
            coord.config["weekly_reminder_snoozed_until"] = v.isoformat() if v else None
        if "zone_order" in data:
            # Store the full ordered list as-is; the panel handles
            # appending any zones not in the list (newly-added ones).
            coord.config["zone_order"] = list(data["zone_order"])
        await coord.async_save_config()
        _LOGGER.info("General config updated: %s", data)

    hass.services.async_register(
        DOMAIN,
        "set_general_config",
        handle_set_general_config,
        schema=_SET_GENERAL_CONFIG_SCHEMA,
    )

    async def handle_start_establishment(call: ServiceCall) -> None:
        from datetime import date, time, timedelta

        data = _START_ESTABLISHMENT_SCHEMA(dict(call.data))
        coord = _find_coordinator(hass)
        if coord is None:
            return

        zone_id = data["zone_entity_id"]
        cycles = data["cycles_per_day"]
        minutes = data["minutes_per_cycle"]
        days = data["days"]
        start_hour = data["start_hour"]

        # Evenly distribute cycles across a 12-hour daytime window
        end_date_val = date.today() + timedelta(days=days)
        cycle_spacing = max(1, 12 // cycles)
        for i in range(cycles):
            hour = (start_hour + i * cycle_spacing) % 24
            sched = Schedule(
                id=uuid.uuid4().hex[:12],
                name=f"New planting {zone_id.split('.', 1)[-1]} cycle {i + 1}",
                zone_entity_id=zone_id,
                start_time=time(hour, 0),
                duration_minutes=minutes,
                weekdays=(0, 1, 2, 3, 4, 5, 6),  # daily
                enabled=True,
                end_date=end_date_val,
                created_via="establishment",
            )
            coord.schedule_store.add(sched)

        await coord.async_save()
        _LOGGER.info(
            "Establishment mode started: %d cycles/day x %d min for %d days on %s (until %s)",
            cycles,
            minutes,
            days,
            zone_id,
            end_date_val.isoformat(),
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_START_ESTABLISHMENT,
        handle_start_establishment,
        schema=_START_ESTABLISHMENT_SCHEMA,
    )

    async def handle_clear_run_history(_call: ServiceCall) -> None:
        coord = _find_coordinator(hass)
        if coord is None:
            return
        removed = coord.run_history.clear()
        await coord.async_save_run_history()
        _LOGGER.info("Cleared %d run-history record(s)", removed)

    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEAR_RUN_HISTORY,
        handle_clear_run_history,
        schema=vol.Schema({}),
    )


def _async_unregister_services(hass: HomeAssistant) -> None:
    """Tear down services. Called when the last config entry unloads."""
    for svc in (
        SERVICE_RUN_ZONE,
        SERVICE_STOP_ZONE,
        SERVICE_CLEAR_RUN_HISTORY,
        SERVICE_ADD_SCHEDULE,
        SERVICE_UPDATE_SCHEDULE,
        SERVICE_DELETE_SCHEDULE,
        SERVICE_SET_SCHEDULE_ENABLED,
        SERVICE_SET_WEATHER_CONFIG,
        SERVICE_SET_ZONE_MOISTURE,
        SERVICE_CLEAR_RAIN_LOCKOUT,
        SERVICE_SET_NOTIFICATION_CONFIG,
        SERVICE_TEST_NOTIFICATION,
        SERVICE_START_ESTABLISHMENT,
    ):
        if hass.services.has_service(DOMAIN, svc):
            hass.services.async_remove(DOMAIN, svc)


def _cleanup_entry_handles(entry_data: dict[str, Any]) -> None:
    """Cancel all timers + listeners for an entry being unloaded."""
    handles = entry_data.get("cancel_handles", {})
    for cancel_timer, cancel_listener in list(handles.values()):
        if cancel_timer:
            cancel_timer()
        if cancel_listener:
            cancel_listener()
    handles.clear()
