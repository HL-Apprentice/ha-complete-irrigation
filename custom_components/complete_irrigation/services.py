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
from .schedule import Schedule

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall

_LOGGER = logging.getLogger(__name__)

SERVICE_RUN_ZONE = "run_zone"
SERVICE_STOP_ZONE = "stop_zone"
SERVICE_ADD_SCHEDULE = "add_schedule"
SERVICE_UPDATE_SCHEDULE = "update_schedule"
SERVICE_DELETE_SCHEDULE = "delete_schedule"
SERVICE_SET_SCHEDULE_ENABLED = "set_schedule_enabled"
SERVICE_SET_WEATHER_CONFIG = "set_weather_config"
SERVICE_SET_ZONE_MOISTURE = "set_zone_moisture"
SERVICE_CLEAR_RAIN_LOCKOUT = "clear_rain_lockout"
SERVICE_SET_NOTIFICATION_CONFIG = "set_notification_config"
SERVICE_TEST_NOTIFICATION = "test_notification"

MAX_SCHEDULE_DURATION_MIN = 240  # 4 hours — safety cap for scheduled runs

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

_ADD_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Required("name"): vol.All(cv.string, vol.Length(min=1, max=80)),
        vol.Required("zone_entity_id"): cv.entity_id,
        vol.Required("start_time"): cv.time,
        vol.Required("duration_minutes"): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=MAX_SCHEDULE_DURATION_MIN)
        ),
        vol.Required("weekdays"): _WEEKDAYS_SCHEMA,
        vol.Optional("enabled", default=True): cv.boolean,
    }
)

_UPDATE_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Required("schedule_id"): cv.string,
        vol.Optional("name"): vol.All(cv.string, vol.Length(min=1, max=80)),
        vol.Optional("zone_entity_id"): cv.entity_id,
        vol.Optional("start_time"): cv.time,
        vol.Optional("duration_minutes"): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=MAX_SCHEDULE_DURATION_MIN)
        ),
        vol.Optional("weekdays"): _WEEKDAYS_SCHEMA,
        vol.Optional("enabled"): cv.boolean,
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
        vol.Optional("rain_sensor"): cv.entity_id,
        vol.Optional("temperature_sensor"): cv.entity_id,
        vol.Optional("hot_threshold_f"): vol.All(vol.Coerce(float), vol.Range(min=50, max=130)),
        vol.Optional("boost_percent"): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
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
        vol.Optional("category"): cv.string,
    }
)

_CLEAR_RAIN_LOCKOUT_SCHEMA = vol.Schema({})

_SET_NOTIFICATION_CONFIG_SCHEMA = vol.Schema(
    {
        vol.Optional("notify_target"): cv.string,
        vol.Optional("quiet_hours_start"): cv.string,
        vol.Optional("quiet_hours_end"): cv.string,
        vol.Optional("enabled"): cv.boolean,
    }
)

_TEST_NOTIFICATION_SCHEMA = vol.Schema({vol.Optional("message", default="Test"): cv.string})


def _find_coordinator(hass: HomeAssistant):
    """Return the first (and currently only) ScheduleCoordinator. v0.x
    limits to one config entry; multi-controller support comes later."""
    from . import _SHARED_KEY

    for key, data in hass.data.get(DOMAIN, {}).items():
        if key == _SHARED_KEY:
            continue
        coord = data.get("coordinator")
        if coord is not None:
            return coord
    return None


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

        # Record the run + turn the switch on.
        now = dt_util.utcnow()
        run = ManualRun(
            entity_id=entity_id,
            start_at=now,
            duration=timedelta(minutes=minutes),
        )
        entry_data["manual_runs"].start(run)

        await hass.services.async_call(
            "switch",
            SERVICE_TURN_ON,
            {"entity_id": entity_id},
            blocking=False,
        )

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
        _LOGGER.info("Stopped manual run for %s by request", entity_id)

    # ── Schedule CRUD ──────────────────────────────────────────────

    async def handle_add_schedule(call: ServiceCall) -> None:
        data = _ADD_SCHEDULE_SCHEMA(dict(call.data))
        coord = _find_coordinator(hass)
        if coord is None:
            _LOGGER.warning("add_schedule called but no coordinator available")
            return
        schedule = Schedule(
            id=uuid.uuid4().hex[:12],
            name=data["name"],
            zone_entity_id=data["zone_entity_id"],
            start_time=data["start_time"],
            duration_minutes=data["duration_minutes"],
            weekdays=tuple(sorted(data["weekdays"])),
            enabled=data["enabled"],
        )
        coord.schedule_store.add(schedule)
        await coord.async_save()
        _LOGGER.info("Added schedule %s (%s)", schedule.id, schedule.name)

    async def handle_update_schedule(call: ServiceCall) -> None:
        data = _UPDATE_SCHEDULE_SCHEMA(dict(call.data))
        coord = _find_coordinator(hass)
        if coord is None:
            return
        existing = coord.schedule_store.get(data["schedule_id"])
        if existing is None:
            _LOGGER.warning("update_schedule: no such schedule %s", data["schedule_id"])
            return
        # Build a new Schedule with overrides applied (frozen dataclass).
        merged = Schedule(
            id=existing.id,
            name=data.get("name", existing.name),
            zone_entity_id=data.get("zone_entity_id", existing.zone_entity_id),
            start_time=data.get("start_time", existing.start_time),
            duration_minutes=data.get("duration_minutes", existing.duration_minutes),
            weekdays=(tuple(sorted(data["weekdays"])) if "weekdays" in data else existing.weekdays),
            enabled=data.get("enabled", existing.enabled),
        )
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
        new = Schedule(
            id=existing.id,
            name=existing.name,
            zone_entity_id=existing.zone_entity_id,
            start_time=existing.start_time,
            duration_minutes=existing.duration_minutes,
            weekdays=existing.weekdays,
            enabled=data["enabled"],
        )
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
        for key in ("rain_sensor", "temperature_sensor", "hot_threshold_f", "boost_percent"):
            if key in data:
                coord.config[key] = data[key]
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


def _async_unregister_services(hass: HomeAssistant) -> None:
    """Tear down services. Called when the last config entry unloads."""
    for svc in (
        SERVICE_RUN_ZONE,
        SERVICE_STOP_ZONE,
        SERVICE_ADD_SCHEDULE,
        SERVICE_UPDATE_SCHEDULE,
        SERVICE_DELETE_SCHEDULE,
        SERVICE_SET_SCHEDULE_ENABLED,
        SERVICE_SET_WEATHER_CONFIG,
        SERVICE_SET_ZONE_MOISTURE,
        SERVICE_CLEAR_RAIN_LOCKOUT,
        SERVICE_SET_NOTIFICATION_CONFIG,
        SERVICE_TEST_NOTIFICATION,
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
