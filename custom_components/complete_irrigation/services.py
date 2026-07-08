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
from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.const import SERVICE_TURN_OFF, SERVICE_TURN_ON
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.util import dt as dt_util

from .care_tasks import (
    CARE_KINDS,
    CARE_PLAN_PRESETS,
    MAX_INTERVAL_DAYS,
    MIN_INTERVAL_DAYS,
    CareTask,
    seed_care_plan,
)
from .chunked_run import (
    DEFAULT_BLOCK_GAP_SECONDS,
    DEFAULT_CONTROLLER_MAX_RUN_MIN,
    plan_blocks,
)
from .const import DEFAULT_MANUAL_RUN_MINUTES, DOMAIN, MAX_SCHEDULE_DURATION_MIN
from .hydraulics import WUCOLS_FACTORS
from .light_survey import (
    DEFAULT_SURVEY_MINUTES,
    MAX_SURVEY_MINUTES,
    MIN_SURVEY_MINUTES,
    validate_lux_range,
)
from .manual_run import ManualRun, validate_run_duration
from .notifications import CATEGORY_CRITICAL  # v1.16: promoted from inline imports
from .plant import PlantRecord
from .run_guard import (
    EXTERNAL_OFF_DEBOUNCE_SECONDS,
    EXTERNAL_OFF_HEALTHY_RUN_SECONDS,
    EXTERNAL_OFF_MAX_REASSERTS,
    external_off_decision,
)
from .run_history import SOURCE_MANUAL, SOURCE_SCHEDULED
from .schedule import Schedule, ZoneStep

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall

_LOGGER = logging.getLogger(__name__)

# v1.21 — external-off resilience. A zone reporting "off" mid-run is
# debounced before we react (cloud integrations like Rachio flap "off" for
# seconds after a turn-on); only if it's persistently off do we re-assert
# the switch on, then finally abort + alert. Use CI's Stop button to stop a
# CI-managed run — a manual off elsewhere may be re-asserted while the run's
# deadline stands. Decision logic lives in run_guard.py (pure, tested).
_EXTERNAL_OFF_DEBOUNCE_SECONDS = EXTERNAL_OFF_DEBOUNCE_SECONDS
_EXTERNAL_OFF_MAX_REASSERTS = EXTERNAL_OFF_MAX_REASSERTS
_EXTERNAL_OFF_HEALTHY_RUN_SECONDS = EXTERNAL_OFF_HEALTHY_RUN_SECONDS
_external_off_decision = external_off_decision

SERVICE_RUN_ZONE = "run_zone"
SERVICE_STOP_ZONE = "stop_zone"
SERVICE_RUN_SCHEDULE = "run_schedule"  # v1.17.13 — "Run schedule now" button
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
SERVICE_SET_CONFLICT_POLICY = "set_conflict_policy"
SERVICE_SET_GENERAL_CONFIG = "set_general_config"
# v2 — plant-aware irrigation
SERVICE_ADD_PLANT = "add_plant"
SERVICE_UPDATE_PLANT = "update_plant"
SERVICE_DELETE_PLANT = "delete_plant"
SERVICE_SET_YARD_MAP = "set_yard_map"  # v1.30 — fetch + cache the aerial backdrop
SERVICE_ADD_PLANT_PHOTO = "add_plant_photo"  # v1.32 — per-plant photo + EXIF-GPS place
SERVICE_SET_PLANT_HEALTH = "set_plant_health"  # v1.33 — store a vision-health verdict
# v1.35 — light surveys + care-task reminders
SERVICE_SET_PLANT_LIGHT_RANGE = "set_plant_light_range"
SERVICE_START_LIGHT_SURVEY = "start_light_survey"
SERVICE_CANCEL_LIGHT_SURVEY = "cancel_light_survey"
SERVICE_ADD_CARE_TASK = "add_care_task"
SERVICE_UPDATE_CARE_TASK = "update_care_task"
SERVICE_DELETE_CARE_TASK = "delete_care_task"
SERVICE_COMPLETE_CARE_TASK = "complete_care_task"
SERVICE_SEED_CARE_PLAN = "seed_care_plan"  # v1.36 — one-tap plan from a preset

_HHMM_RE = r"^([01]\d|2[0-3]):[0-5]\d$"  # quiet-hours 24h time, validated at the boundary

# Voluptuous schema for validation at the HA boundary.
# v1.24 — CRITICAL: cap at MAX_SCHEDULE_DURATION_MIN, NOT a 60-min manual cap.
# run_zone is the single entry point for BOTH manual runs AND scheduled runs
# (coordinator._fire_run dispatches every scheduled run through it). A 60-min
# cap here silently rejected every schedule longer than 60 min — the long
# deep-watering zones (Shrubs 135, Citrus 240, Trees 300) never fired at all.
_RUN_ZONE_SCHEMA = vol.Schema(
    {
        vol.Required("entity_id"): cv.entity_id,
        vol.Optional("minutes", default=DEFAULT_MANUAL_RUN_MINUTES): vol.All(
            vol.Coerce(float),
            vol.Range(min=1, max=MAX_SCHEDULE_DURATION_MIN),
        ),
    }
)

_STOP_ZONE_SCHEMA = vol.Schema(
    {
        vol.Required("entity_id"): cv.entity_id,
    }
)

# v1.17.5 — dropped vol.Length(min=1). The mode-specific "weekdays
# required when mode=weekdays" check happens at Schedule.__post_init__
# (see _validate_weekdays_mode). Enforcing it at the schema layer
# blocked legitimate updates to interval / interval_hours schedules:
# the panel correctly sends `weekdays: []` for those modes and the
# strict schema would 400 the request. Mirrors the permissive shape
# already used in _ADD_SCHEDULE_SCHEMA.
_WEEKDAYS_SCHEMA = vol.All(
    cv.ensure_list,
    [vol.All(vol.Coerce(int), vol.Range(min=0, max=6))],
)

_ZONE_STEP_SCHEMA = vol.Schema(
    {
        vol.Required("zone_entity_id"): cv.entity_id,
        vol.Required("duration_minutes"): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=MAX_SCHEDULE_DURATION_MIN)
        ),
    }
)


def _validate_hex_color(value):
    """v1.18 — accept a "#rrggbb" hex color or None/empty (= no color).

    Kept lenient on the exact value (any well-formed 6-digit hex) so a
    future palette change doesn't reject schedules created under the
    old palette; the panel restricts the picker to SCHEDULE_COLOR_PALETTE."""
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise vol.Invalid("color must be a '#rrggbb' string or null")
    v = value.strip().lower()
    if len(v) != 7 or v[0] != "#" or any(c not in "0123456789abcdef" for c in v[1:]):
        raise vol.Invalid(f"color must be a '#rrggbb' hex string, got {value!r}")
    return v


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
        # v1.17.3 — per-schedule weather-gate opt-outs
        vol.Optional("ignore_wind", default=False): cv.boolean,
        vol.Optional("ignore_hot_weather", default=False): cv.boolean,
        vol.Optional("ignore_rain_lockout", default=False): cv.boolean,
        vol.Optional("color"): _validate_hex_color,  # v1.18
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
        vol.Optional("ignore_wind"): cv.boolean,
        vol.Optional("ignore_hot_weather"): cv.boolean,
        vol.Optional("ignore_rain_lockout"): cv.boolean,
        vol.Optional("color"): _validate_hex_color,  # v1.18
        vol.Optional("zone_steps"): vol.All(cv.ensure_list, [_ZONE_STEP_SCHEMA]),
    }
)

_DELETE_SCHEDULE_SCHEMA = vol.Schema({vol.Required("schedule_id"): cv.string})

_RUN_SCHEDULE_SCHEMA = vol.Schema({vol.Required("schedule_id"): cv.string})

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
        # v2 — reference evapotranspiration (in/week) + drip efficiency drive
        # the plant-aware water calculator. eto_in_week is the MANUAL figure;
        # v1.28 eto_auto computes it from the weather forecast (FAO-56) and
        # falls back to this manual value when the feed is missing/stale.
        vol.Optional("eto_in_week"): vol.All(vol.Coerce(float), vol.Range(min=0.1, max=10)),
        vol.Optional("eto_auto"): cv.boolean,
        vol.Optional("weather_entity"): cv.entity_id,
        vol.Optional("drip_efficiency"): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=1.0)),
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
        # v1.18 — fail-closed: skip a scheduled run when NO moisture
        # reading is available for this zone (all sensors offline /
        # unavailable). Default off = legacy fail-open (water anyway).
        vol.Optional("require_moisture_reading"): cv.boolean,
        # v1.18.4 — ignore moisture entirely for watering decisions on
        # this zone. Sensors stay bound (chips / tile stats still show
        # live readings) but the moisture gate becomes a no-op: no
        # saturated-skip, no runtime adjustment. Default off.
        vol.Optional("moisture_disabled"): cv.boolean,
        # v1.19 — per-sensor analysis opt-out. Entities listed here stay
        # bound for DISPLAY (chips, tile stats, tooltips) but are
        # excluded from the combine/average used by the moisture gate
        # and auto-soak. For sensors that consistently read wrong and
        # skew the math.
        vol.Optional("moisture_excluded"): vol.All(cv.ensure_list, [cv.entity_id]),
        # v1.19 — auto-soak recovery. When enabled and the zone's
        # effective moisture drops below min_pct: run soak_run_minutes,
        # wait soak_wait_minutes for absorption, re-read, repeat until
        # moisture >= min_pct or soak_max_cycles is hit (then notify +
        # 6h cooldown so a stuck-low sensor can't water forever).
        vol.Optional("auto_soak_enabled"): cv.boolean,
        vol.Optional("soak_run_minutes"): vol.All(vol.Coerce(int), vol.Range(min=1, max=60)),
        vol.Optional("soak_wait_minutes"): vol.All(vol.Coerce(int), vol.Range(min=5, max=240)),
        vol.Optional("soak_max_cycles"): vol.All(vol.Coerce(int), vol.Range(min=1, max=10)),
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
        # Validate HH:MM at the boundary so a malformed value can't reach the
        # parse sinks (notify hot path + coordinator summary scheduling).
        vol.Optional("quiet_hours_start"): vol.All(cv.string, vol.Match(_HHMM_RE)),
        vol.Optional("quiet_hours_end"): vol.All(cv.string, vol.Match(_HHMM_RE)),
        vol.Optional("enabled"): cv.boolean,
        # Daily summary fires at quiet-hours-end if any zone is below its
        # configured min%. Defaults to true so it just works after setup.
        vol.Optional("low_moisture_alerts"): cv.boolean,
        # v1.17 — when a scheduled run is dropped (conflict resolver,
        # moisture/wind/rain gate, or missed due to HA restart), send
        # a notification with a "Run now" action button so the user
        # can recover the missed run with one tap.
        vol.Optional("notify_on_missed"): cv.boolean,
        # v1.17.8 — when a scheduled run is cut short by an external
        # actor (something other than our auto-stop turns the switch
        # off), notify with "Run remainder" + "Open Logbook" actions.
        vol.Optional("notify_on_aborted"): cv.boolean,
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
        # v1.17.11 — when true, every data-mutating / hardware-actuating
        # service call (run_zone, stop_zone, add/update/delete_schedule,
        # etc.) requires the calling user to be an HA admin. Default
        # false so existing scripts/automations running as non-admin
        # contexts don't suddenly start failing.
        vol.Optional("admin_only_services"): cv.boolean,
        # v1.25 — controller per-activation cap (block size) + inter-block gap
        # for long-run chunking. Defaults (58 min / 30 s) suit Rachio; tune only
        # if your controller's manual-run limit differs.
        vol.Optional("controller_max_run_minutes"): vol.All(
            vol.Coerce(int), vol.Range(min=5, max=480)
        ),
        vol.Optional("block_gap_seconds"): vol.All(vol.Coerce(int), vol.Range(min=5, max=600)),
    }
)

# v1.16 — table-driven persistence for set_general_config. Each key in
# the schema above maps to a transform applied before storing into
# coord.config. Plain pass-through is the default; specialized entries
# (e.g. date → isoformat) live here. Adding a new general-config field
# means one schema line + (if non-trivial) one entry here.
_GENERAL_CONFIG_FIELDS: dict[str, Any] = {
    "zone_buffer_seconds": lambda v: v,
    "weekly_reminder_snoozed_until": lambda v: v.isoformat() if v else None,
    "zone_order": lambda v: list(v),
    "admin_only_services": bool,  # v1.17.11
    "controller_max_run_minutes": int,  # v1.25
    "block_gap_seconds": int,  # v1.25
}


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

# v2 — plant CRUD. canopy_area_sqft is generous-bounded (a small annual to a
# big shade tree's canopy); wucols_category is constrained to the known set.
_WUCOLS_CATEGORIES = tuple(sorted(WUCOLS_FACTORS))
_CANOPY_AREA = vol.All(vol.Coerce(float), vol.Range(min=0.1, max=100000))

_SPECIES = vol.All(cv.string, vol.Length(max=120), _no_control_chars)

_ADD_PLANT_SCHEMA = vol.Schema(
    {
        vol.Required("name"): vol.All(cv.string, vol.Length(min=1, max=80), _no_control_chars),
        vol.Required("wucols_category"): vol.In(_WUCOLS_CATEGORIES),
        vol.Required("canopy_area_sqft"): _CANOPY_AREA,
        vol.Required("zone_entity_id"): cv.entity_id,
        # v1.35 — optional species (common or scientific name, free text).
        vol.Optional("species", default=""): _SPECIES,
    }
)

_MAP_COORD = vol.All(vol.Coerce(float), vol.Range(min=0.0, max=1.0))

_UPDATE_PLANT_SCHEMA = vol.Schema(
    {
        vol.Required("plant_id"): cv.string,
        vol.Optional("name"): vol.All(cv.string, vol.Length(min=1, max=80), _no_control_chars),
        vol.Optional("wucols_category"): vol.In(_WUCOLS_CATEGORIES),
        vol.Optional("canopy_area_sqft"): _CANOPY_AREA,
        vol.Optional("zone_entity_id"): cv.entity_id,
        # v1.30 — normalized yard-map position (set when a marker is dragged/placed).
        vol.Optional("map_x"): _MAP_COORD,
        vol.Optional("map_y"): _MAP_COORD,
        # v1.35 — optional species (common or scientific name, free text).
        vol.Optional("species"): _SPECIES,
    }
)

_DELETE_PLANT_SCHEMA = vol.Schema({vol.Required("plant_id"): cv.string})

# v1.30 — fetch + cache the aerial backdrop. Center defaults to the HA location.
_SET_YARD_MAP_SCHEMA = vol.Schema(
    {
        vol.Optional("latitude"): vol.All(vol.Coerce(float), vol.Range(min=-90, max=90)),
        vol.Optional("longitude"): vol.All(vol.Coerce(float), vol.Range(min=-180, max=180)),
        vol.Optional("span_m"): vol.All(vol.Coerce(float), vol.Range(min=10, max=500)),
    }
)

# v1.32 — attach a photo to a plant (for biannual LLM-vision health checks). The
# image arrives base64-encoded (the panel downsizes it client-side first). lat/lon
# are the photo's EXIF GPS, used to AUTO-PLACE the marker only if the plant is still
# unplaced — per the locked workflow, GPS places ONCE; selection (plant_id) owns
# identity on every update, manual drag owns position.
_MAX_PHOTO_B64 = 12_000_000  # ~9 MB image; the panel sends much smaller (downsized)
_MAX_PHOTOS_PER_PLANT = 24
_ADD_PLANT_PHOTO_SCHEMA = vol.Schema(
    {
        vol.Required("plant_id"): cv.string,
        vol.Required("image_base64"): vol.All(cv.string, vol.Length(min=64, max=_MAX_PHOTO_B64)),
        vol.Optional("latitude"): vol.All(vol.Coerce(float), vol.Range(min=-90, max=90)),
        vol.Optional("longitude"): vol.All(vol.Coerce(float), vol.Range(min=-180, max=180)),
        vol.Optional("note", default=""): vol.All(cv.string, vol.Length(max=200)),
    }
)
# v1.33 — the NAS vision job posts its raw verdict here; the vision_health rail bounds
# it before storage. `verdict` is the model's dict (health_state/confidence/…).
_SET_PLANT_HEALTH_SCHEMA = vol.Schema(
    {
        vol.Required("plant_id"): cv.string,
        vol.Required("verdict"): dict,
        vol.Optional("model", default=""): vol.All(cv.string, vol.Length(max=80)),
    }
)

# v1.35 — per-plant optimal-lux range. Either both bounds (validated against each
# other by light_survey.validate_lux_range in the handler) or clear=true to unset.
_SET_PLANT_LIGHT_RANGE_SCHEMA = vol.Schema(
    {
        vol.Required("plant_id"): cv.string,
        vol.Optional("lux_low"): vol.Coerce(int),
        vol.Optional("lux_high"): vol.Coerce(int),
        vol.Optional("clear", default=False): cv.boolean,
    }
)

# v1.35 — roaming light survey: sample an illuminance sensor placed at the plant.
_START_LIGHT_SURVEY_SCHEMA = vol.Schema(
    {
        vol.Required("plant_id"): cv.string,
        vol.Required("sensor_entity_id"): cv.entity_id,
        vol.Optional("minutes", default=DEFAULT_SURVEY_MINUTES): vol.All(
            vol.Coerce(int), vol.Range(min=MIN_SURVEY_MINUTES, max=MAX_SURVEY_MINUTES)
        ),
    }
)
_CANCEL_LIGHT_SURVEY_SCHEMA = vol.Schema({vol.Required("plant_id"): cv.string})

# v1.35 — care-task reminders (fertilize/prune/mulch/inspect/custom).
_CARE_INTERVAL = vol.All(vol.Coerce(int), vol.Range(min=MIN_INTERVAL_DAYS, max=MAX_INTERVAL_DAYS))
_CARE_LABEL = vol.All(cv.string, vol.Length(max=120), _no_control_chars)
_ADD_CARE_TASK_SCHEMA = vol.Schema(
    {
        vol.Required("kind"): vol.In(CARE_KINDS),
        vol.Required("interval_days"): _CARE_INTERVAL,
        vol.Optional("plant_id", default=""): cv.string,
        # Empty (task is plant-scoped) or a real entity id — same validator every
        # other zone field in this file uses; the raw string is echoed into
        # notifications, so it must be format-bounded here.
        vol.Optional("zone_entity_id", default=""): vol.Any("", cv.entity_id),
        vol.Optional("label", default=""): _CARE_LABEL,
    }
)
_UPDATE_CARE_TASK_SCHEMA = vol.Schema(
    {
        vol.Required("task_id"): cv.string,
        vol.Optional("kind"): vol.In(CARE_KINDS),
        vol.Optional("interval_days"): _CARE_INTERVAL,
        vol.Optional("label"): _CARE_LABEL,
        vol.Optional("enabled"): cv.boolean,
    }
)
_DELETE_CARE_TASK_SCHEMA = vol.Schema({vol.Required("task_id"): cv.string})
_COMPLETE_CARE_TASK_SCHEMA = vol.Schema({vol.Required("task_id"): cv.string})
# v1.36 — one-tap care-plan seeding from a plant-type preset.
_SEED_CARE_PLAN_SCHEMA = vol.Schema(
    {
        vol.Required("plant_id"): cv.string,
        vol.Required("preset"): vol.In(sorted(CARE_PLAN_PRESETS)),
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


async def _require_admin_if_configured(hass: HomeAssistant, call) -> bool:
    """v1.17.11 — opt-in admin gate for hardware/CRUD service calls.

    When `admin_only_services` is true in the coordinator config,
    rejects calls whose `call.context.user_id` resolves to a non-admin
    HA user. System-initiated calls (no user_id — e.g. the integration's
    own RUN_MISSED / RUN_REMAINDER notification action handlers calling
    run_zone internally) always pass through; the gate is for user-
    originated calls from the panel UI, Developer Tools, or scripts
    running under a user context.

    Returns True if the call should proceed, False if it was rejected
    (and a warning was logged). Handlers should early-return on False.

    Default off so existing setups with non-admin scripts/automations
    don't suddenly break. Users with shared HA installs flip it on in
    Settings → Restrict services to admin users.
    """
    from . import find_coordinator

    coord = find_coordinator(hass)
    if coord is None:
        return True  # no coordinator → can't enforce; fail open
    if not coord.config.get("admin_only_services"):
        return True  # feature disabled
    user_id = getattr(call.context, "user_id", None)
    if not user_id:
        return True  # system / no-user context (action handlers, automations)
    user = await hass.auth.async_get_user(user_id)
    if user and user.is_admin:
        return True
    _LOGGER.warning(
        "Rejected %s.%s call from non-admin user %s (admin_only_services=on)",
        call.domain,
        call.service,
        user_id,
    )
    return False


async def _require_admin(hass: HomeAssistant, call) -> bool:
    """v1.32 — UNCONDITIONAL admin gate, independent of admin_only_services.

    Used by the services that write arbitrary file BYTES to disk and/or make
    an outbound HTTP fetch (add_plant_photo, set_yard_map). Those are the
    integration's only arbitrary-file-write + outbound-request surface and have
    no legitimate non-admin use case, so a low-privileged authenticated user
    must never reach them (closes the path-traversal / stored-file / disk-DoS
    vectors at the access layer). System-initiated calls (no user_id) still
    pass — only an admin can author automations/scripts in HA, so a no-user
    context is already admin-authored.

    Returns True to proceed, False if rejected (handlers early-return on False).
    """
    user_id = getattr(call.context, "user_id", None)
    if not user_id:
        return True  # system / automation context (admin-authored in HA)
    user = await hass.auth.async_get_user(user_id)
    if user and user.is_admin:
        return True
    _LOGGER.warning(
        "Rejected %s.%s from non-admin user %s (admin-only: writes files / fetches URLs)",
        call.domain,
        call.service,
        user_id,
    )
    return False


# Magic-byte signatures for the image formats a browser will render from /local.
# We validate decoded upload bytes against these before writing so a crafted
# SVG/HTML payload can't be stored as a .jpg and served back (content-sniffing /
# stored-XSS vector). Order: JPEG, PNG, GIF87a/89a, WEBP (RIFF....WEBP), BMP.
def _looks_like_image(data: bytes) -> bool:
    if len(data) < 12:
        return False
    if data[:3] == b"\xff\xd8\xff":  # JPEG
        return True
    if data[:8] == b"\x89PNG\r\n\x1a\n":  # PNG
        return True
    if data[:6] in (b"GIF87a", b"GIF89a"):  # GIF
        return True
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":  # WEBP
        return True
    return data[:2] == b"BM"  # BMP


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


def _clear_external_off(entry_data: dict[str, Any], entity_id: str) -> None:
    """Cancel a pending debounce check and drop the per-zone external-off
    state. Called whenever a run ends (auto-stop / Stop button / abort)."""
    ext = entry_data.get("external_off", {}).pop(entity_id, None)
    if ext and ext.get("pending"):
        ext["pending"]()


def _cancel_block_timers(entry_data: dict[str, Any], entity_id: str) -> None:
    """v1.25 — cancel any pending chunked-run block timers for this zone and
    invalidate the sequence.

    A long run is delivered as back-to-back blocks scheduled with
    async_call_later (see _dispatch_run_blocks). Without this, Stopping a zone
    turned the valve off only for the *current* block while the queued later
    blocks re-opened it — runaway re-watering after the user pressed Stop. Every
    path that ends a run (Stop / delete_schedule / unload) and every new sequence
    on the same zone calls this. We ADVANCE the generation FIRST so any block
    timer already slipped onto the event loop becomes a no-op when it fires (it
    re-checks the generation), then cancel the still-pending timers. The
    generation only ever advances (never resets), so a superseding sequence can't
    collide with a stale timer's id."""
    seqs = entry_data.setdefault("block_seq", {})
    seqs[entity_id] = seqs.get(entity_id, 0) + 1
    for cancel in entry_data.get("block_timers", {}).pop(entity_id, []):
        try:
            cancel()
        except Exception:
            # A timer that already fired returns a cancel that's a harmless
            # no-op; guard anyway so teardown never raises.
            _LOGGER.debug("block-timer cancel for %s was already fired", entity_id)


async def _notify_zone_failed(coord, friendly: str, message: str) -> None:
    """v1.16 — extracted from handle_run_zone. Sends a critical
    notification when a zone fails to start. No-op if coord or notifier
    is unavailable."""
    if coord is None or coord.notifier is None:
        return
    await coord.notifier.notify(
        message,
        title="Zone failed to start",
        category=CATEGORY_CRITICAL,
        event_type="zone_start_failed",
    )


async def _maybe_notify_cut_short(coord, record) -> None:
    """v1.17.8 — actionable notification when a SCHEDULED run is cut
    short by an external actor at <90% of planned duration.

    Triggered from the _on_state_change listener in handle_run_zone.
    Honors the `notify_on_aborted` setting (default true). Action
    buttons:

      • "Run remainder (N min)" — fires run_zone for the missing
        duration (planned minus actual). Uses the same Companion-app
        mobile_app_notification_action plumbing as v1.17.0.
      • "Open Logbook" — opens HA Logbook filtered to this switch so
        the user can see who/what turned it off (the actual fix path).
    """
    if record is None or coord is None or coord.notifier is None:
        return
    # Manual aborts and user-initiated Stops aren't surprises — don't notify.
    if record.source != SOURCE_SCHEDULED:
        return
    if not record.reason or "externally" not in record.reason:
        return
    requested = int(record.requested_minutes or 0)
    actual = int(record.actual_minutes or 0)
    if requested <= 0:
        return
    # Only notify if the cut was significant (>10% of planned remaining).
    if actual >= requested * 0.9:
        return
    notif_cfg = coord.config.get("notifications", {})
    if not notif_cfg.get("notify_on_aborted", True):
        return
    remaining = max(0, requested - actual)
    await coord.notifier.notify(
        f"{record.schedule_name or 'Scheduled run'} ({record.zone_name}) was cut "
        f"short — ran {actual} of {requested} min before something turned the "
        f"switch off. Tap to run the remaining {remaining} min or check the "
        f"Logbook for the cause.",
        title="Irrigation: run cut short",
        category="important",
        event_type="run_cut_short",
        actions=[
            {
                "action": "COMPLETE_IRRIGATION_RUN_REMAINDER",
                "title": f"Run remainder ({remaining} min)",
                "data": {
                    "zone_entity_id": record.zone_entity_id,
                    "minutes": remaining,
                    "original_record_id": record.id,
                    "schedule_id": record.schedule_id,
                    "schedule_name": record.schedule_name,
                },
            },
            {
                "action": "URI",
                "title": "Open Logbook",
                "uri": f"/config/logs?entity_id={record.zone_entity_id}",
            },
        ],
    )


def _record_run_start_to_history(
    coord,
    *,
    entity_id: str,
    friendly_name: str,
    minutes: int,
    meta: dict[str, Any] | None,
    source: str,
    started_at,
) -> None:
    """v1.16 — extracted from handle_run_zone. Writes the start record
    to coord.run_history and kicks off a save. Scheduled-run meta
    overrides the default zone_name / requested_minutes / triggers so
    the actual-vs-requested column reflects pre-gate planned duration."""
    if coord is None:
        return
    coord.run_history.start_run(
        zone_entity_id=entity_id,
        zone_name=(meta or {}).get("zone_name") or friendly_name,
        requested_minutes=(meta or {}).get("requested_minutes", minutes),
        source=source,
        started_at=started_at,
        schedule_id=(meta or {}).get("schedule_id"),
        schedule_name=(meta or {}).get("schedule_name", ""),
        triggers=(meta or {}).get("triggers") or {},
    )


def _record_active_session(
    hass: HomeAssistant,
    coord,
    *,
    zone: str,
    total_minutes: int,
    started_at,
    meta: dict[str, Any] | None,
    source: str,
    extra_seconds: int = 0,
) -> None:
    """v1.30 restart fail-over — persist the LOGICAL run (its full duration +
    planned end) so an HA restart can resume it. Stored in coordinator config,
    keyed by zone; cleared when the whole run ends. Block sub-calls do NOT call
    this — the parent logical run already covers the session.

    `total_minutes` is the WATER time (the resume cap, so a re-chunk re-adds gaps);
    `extra_seconds` is the inter-block gap wall-clock added to the DEADLINE only, so
    a chunked run's planned end reflects its real (gap-inclusive) completion time."""
    if coord is None:
        return
    # deadline = gap-INCLUSIVE wall-clock end (for the Today card / countdown).
    # water_deadline = gap-FREE end (started + water minutes) used by the restart
    # resume math, so re-added gaps on each resume can never accumulate as extra
    # water across repeated restarts (resume converges to the same water owed).
    deadline = started_at + timedelta(minutes=int(total_minutes), seconds=int(extra_seconds))
    water_deadline = started_at + timedelta(minutes=int(total_minutes))
    coord.config.setdefault("active_run_sessions", {})[zone] = {
        "total_minutes": int(total_minutes),
        "started_at": started_at.isoformat(),
        "deadline": deadline.isoformat(),
        "water_deadline": water_deadline.isoformat(),
        "schedule_id": (meta or {}).get("schedule_id"),
        "schedule_name": (meta or {}).get("schedule_name", ""),
        "source": source,
    }
    hass.async_create_task(coord.async_save_config())


def _clear_active_session(hass: HomeAssistant, coord, zone: str) -> None:
    """Drop the persisted active session for a zone (run finished / stopped /
    aborted) so a later restart doesn't resume an already-done run."""
    if coord is None:
        return
    sessions = coord.config.get("active_run_sessions")
    if sessions and sessions.pop(zone, None) is not None:
        hass.async_create_task(coord.async_save_config())


def _session_decision_for_run(entry_data: dict[str, Any], entity_id: str) -> tuple[bool, bool]:
    """v1.30 — for a run_zone call, return (is_block_subcall, is_final_activation),
    consuming the block marker set by _dispatch_run_blocks.

    - Not a block sub-call -> (False, True): a logical run; record its session and
      its auto-stop clears the session.
    - Block i/n -> (True, i == n): a chunked sub-call; the parent already recorded
      the session, so DON'T re-record; only the final block (i == n) clears it.
    This is the guard that stops a MANUAL chunked run (whose blocks carry no
    schedule name) from overwriting + prematurely clearing its own session."""
    info = entry_data.get("block_subcall", {}).pop(entity_id, None)
    if info:
        return True, int(info["idx"]) == int(info["n"])
    return False, True


def _dispatch_run_blocks(
    hass: HomeAssistant,
    entity_id: str,
    entry_data: dict[str, Any],
    blocks: list[int],
    gap_seconds: int,
    base_meta: dict[str, Any] | None,
) -> None:
    """v1.25 — deliver a >cap run as back-to-back cap-sized blocks (off -> gap ->
    on each time = a fresh activation the controller accepts).

    Block 1 fires now; blocks 2..n are scheduled at increasing offsets so each
    starts after the previous block's water time + the gap. Every block is an
    ordinary <=cap run_zone service call, so it re-uses the normal single-
    activation path (auto-stop + external-off monitor) per block. Scheduled
    metadata is carried into each block so the run history reads "name (block
    i/n)"; manual blocks (no base_meta) just record as ordinary manual runs.

    Cancellation safety: the scheduled-block timer handles are stored in
    entry_data["block_timers"][entity_id] and the sequence is tagged with a
    generation in entry_data["block_seq"][entity_id]. Stop / delete_schedule /
    unload call _cancel_block_timers to cancel the pending timers and drop the
    generation; each block also re-checks the generation when it fires, so a Stop
    truly stops the run instead of letting later blocks re-open the valve. A new
    sequence on the same zone supersedes any prior one.
    """
    # A fresh sequence supersedes any still-pending one on this zone.
    # _cancel_block_timers advances the generation; that advanced value is this
    # sequence's id (each block re-checks it before firing).
    _cancel_block_timers(entry_data, entity_id)
    gen = entry_data["block_seq"][entity_id]
    timers = entry_data.setdefault("block_timers", {})
    timers[entity_id] = []

    n = len(blocks)
    offset = 0
    for i, block_min in enumerate(blocks):

        def _fire(_now=None, bm=block_min, idx=i + 1, seq=gen):
            # Skip if this sequence was cancelled / superseded since the timer was
            # scheduled (Stop, delete_schedule, reload, or a newer run here).
            if entry_data.get("block_seq", {}).get(entity_id) != seq:
                return
            if base_meta is not None:
                meta = dict(base_meta)
                base_name = base_meta.get("schedule_name") or ""
                meta["schedule_name"] = f"{base_name} (block {idx}/{n})"
                meta["requested_minutes"] = bm
                entry_data.setdefault("pending_run_meta", {})[entity_id] = meta
            # v1.30 — mark this run_zone call as a block sub-call so it doesn't
            # re-record / clear the parent's restart-resume session. Set for the
            # MANUAL case too (base_meta is None there, so the "(block i/n)" name
            # isn't present) — otherwise a manual chunked run's session would be
            # overwritten with one block + cleared after block 1.
            entry_data.setdefault("block_subcall", {})[entity_id] = {"idx": idx, "n": n}
            hass.async_create_task(
                hass.services.async_call(
                    DOMAIN,
                    SERVICE_RUN_ZONE,
                    {"entity_id": entity_id, "minutes": bm},
                    blocking=False,
                )
            )

        if i == 0:
            _fire()
        else:
            timers[entity_id].append(async_call_later(hass, offset, _fire))
        offset += block_min * 60 + gap_seconds


async def _async_register_services(hass: HomeAssistant) -> None:
    """Register both services. Idempotent — safe to call from multiple entries."""

    if hass.services.has_service(DOMAIN, SERVICE_RUN_ZONE):
        return  # already registered

    async def handle_run_zone(call: ServiceCall) -> None:
        if not await _require_admin_if_configured(hass, call):
            return
        data = _RUN_ZONE_SCHEMA(dict(call.data))
        entity_id: str = data["entity_id"]
        try:
            # v1.24 — cap at the schedule max (480), not 60: scheduled long
            # runs flow through here too and must not be truncated/rejected.
            minutes = validate_run_duration(data["minutes"], MAX_SCHEDULE_DURATION_MIN)
        except (TypeError, ValueError) as err:
            raise vol.Invalid(str(err)) from err

        entry_data = _find_entry_data(hass, entity_id)
        if entry_data is None:
            _LOGGER.warning(
                "run_zone called for %s, not a configured zone — ignoring",
                entity_id,
            )
            return

        # v1.25 — controllers (Rachio) cap each zone activation at a fixed number
        # of minutes; a longer run can't be delivered in one activation. Split it
        # into back-to-back blocks (each <= the cap) with a short gap between
        # (off -> gap -> on = a fresh activation each time). Both scheduled
        # (_fire_run) and manual runs funnel through here, so this is the single
        # place chunking belongs. A run within the cap falls through to the normal
        # single-activation path below, UNCHANGED.
        cfg = getattr(entry_data.get("coordinator"), "config", None) or {}
        cap = int(cfg.get("controller_max_run_minutes", DEFAULT_CONTROLLER_MAX_RUN_MIN))
        blocks = plan_blocks(minutes, cap)
        if len(blocks) > 1:
            gap_s = int(cfg.get("block_gap_seconds", DEFAULT_BLOCK_GAP_SECONDS))
            base_meta = entry_data.get("pending_run_meta", {}).pop(entity_id, None)
            # v1.30 restart fail-over — record the session for the WHOLE chunked
            # run (full minutes) before dispatching blocks, so a restart mid-
            # session resumes the remaining total time, not just the current block.
            _record_active_session(
                hass,
                entry_data.get("coordinator"),
                zone=entity_id,
                total_minutes=minutes,
                started_at=dt_util.utcnow(),
                meta=base_meta,
                source=SOURCE_SCHEDULED if base_meta else SOURCE_MANUAL,
                # The real completion is later than the water minutes by the gaps
                # between the n blocks — fold them into the deadline.
                extra_seconds=(len(blocks) - 1) * gap_s,
            )
            _LOGGER.info(
                "%s (%s min) exceeds the %d-min controller cap — delivering as "
                "%d blocks with a %ds gap",
                entity_id,
                minutes,
                cap,
                len(blocks),
                gap_s,
            )
            _dispatch_run_blocks(hass, entity_id, entry_data, blocks, gap_s, base_meta)
            return

        # Replace any existing run for this entity (cancel old timer + listener
        # + any pending external-off debounce).
        _cancel_handles(entry_data, entity_id)
        _clear_external_off(entry_data, entity_id)

        # v1.30 — decide session record-vs-skip up front (consumes the block
        # marker). A NON-block (logical) run must SUPERSEDE any in-flight CHUNKED
        # run on this zone: cancel its pending block timers (advance the
        # generation) so its orphaned blocks become no-ops instead of re-opening
        # the valve untracked after this run clears the shared session. A block
        # sub-call must NOT cancel — those pending timers are its own siblings.
        _is_block_subcall, is_final_activation = _session_decision_for_run(entry_data, entity_id)
        if not _is_block_subcall:
            _cancel_block_timers(entry_data, entity_id)

        # Hard-failure guard (PRD #43): if the underlying switch entity is
        # missing or already unavailable, refuse to run and notify the user
        # immediately instead of silently logging.
        state = hass.states.get(entity_id)
        coord = entry_data.get("coordinator")
        friendly = (state.attributes.get("friendly_name") if state else None) or entity_id
        if state is None or state.state == "unavailable":
            _LOGGER.warning(
                "run_zone: entity %s is %s — refusing to start",
                entity_id,
                "missing" if state is None else "unavailable",
            )
            await _notify_zone_failed(
                coord,
                friendly,
                f"Cannot start {friendly} — the zone switch is "
                f"{'missing from HA' if state is None else 'unavailable'}.",
            )
            # v1.30 — drop any stashed meta so a refused start can't misattribute
            # the next run. Do NOT clear the active session here: a restart RESUME
            # that arrives before the switch is back online would otherwise lose
            # the run entirely. The resume path gates on switch availability +
            # retries instead (see coordinator._resume_interrupted_runs); a stale
            # session self-expires at its deadline.
            entry_data.get("pending_run_meta", {}).pop(entity_id, None)
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
        # run) — if present, this is a scheduled call; otherwise manual. v1.30 —
        # a restart resume stashes the ORIGINAL source so a resumed manual run
        # stays "manual" in history (don't force scheduled just because meta is set).
        meta = entry_data.get("pending_run_meta", {}).pop(entity_id, None)
        source = (meta or {}).get("source") or (SOURCE_SCHEDULED if meta else SOURCE_MANUAL)

        # v1.30 restart fail-over — record the session for the LOGICAL run (the
        # record-vs-skip + supersede decision was made above). A block sub-call
        # belongs to a parent session already recorded by the chunking branch.
        if not _is_block_subcall:
            _record_active_session(
                hass,
                coord,
                zone=entity_id,
                total_minutes=minutes,
                started_at=now,
                meta=meta,
                source=source,
            )

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
            await _notify_zone_failed(coord, friendly, f"Zone {friendly} failed to start: {err}")
            return

        # ── Run history: record the start ──
        # requested_minutes for scheduled runs uses the ORIGINAL planned
        # duration (before boost), so actual_minutes vs requested reflects
        # gate adjustments. Manual runs just use the service call's minutes.
        _record_run_start_to_history(
            coord,
            entity_id=entity_id,
            friendly_name=friendly,
            minutes=minutes,
            meta=meta,
            source=source,
            started_at=now,
        )
        if coord is not None:
            hass.async_create_task(coord.async_save_run_history())

        # ── auto-stop timer ──
        async def _auto_stop(_now=None):
            run_now = entry_data["manual_runs"].get(entity_id)
            if run_now is None:
                return  # already cleaned up by an external off
            entry_data["manual_runs"].stop(entity_id)
            _clear_external_off(entry_data, entity_id)
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
            # v1.30 — clear the restart-resume session only when the WHOLE run is
            # done (a single run, or the final block of a chunked run); an
            # intermediate block keeps the session so a restart still resumes.
            if is_final_activation:
                _clear_active_session(hass, coord, entity_id)
            _LOGGER.info("Manual run for %s expired and was stopped", entity_id)

        cancel_timer = async_call_later(hass, minutes * 60, _auto_stop)

        # ── debounced external-off verification ──
        # Fires `_EXTERNAL_OFF_DEBOUNCE_SECONDS` after a zone reports off
        # mid-run. By now a stale cloud poll has usually been corrected, so
        # we re-read the live state and only then decide (see
        # _external_off_decision): recovered → continue; persistently off →
        # re-assert the switch on, or abort + alert once attempts run out.
        async def _verify_external_off(_now=None):
            ext = entry_data.get("external_off", {}).get(entity_id)
            if ext is not None:
                ext["pending"] = None
            run_now = entry_data["manual_runs"].get(entity_id)
            if run_now is None:
                return  # run already ended (auto-stop / Stop) while we waited
            state = hass.states.get(entity_id)
            state_now = state.state if state is not None else "unavailable"
            reasserts_left = ext.get("reasserts_left", 0) if ext else 0
            before_deadline = dt_util.utcnow() < run_now.deadline()
            ran_healthy = bool(ext.get("ran_healthy")) if ext else False
            decision = _external_off_decision(
                state_now, reasserts_left, before_deadline, ran_healthy
            )

            if decision == "recovered":
                _LOGGER.info(
                    "Zone %s reported off transiently but is back on — run continues",
                    entity_id,
                )
                return
            if decision in ("retry", "retry_reset"):
                if ext is not None:
                    if decision == "retry_reset":
                        # Legit controller cap-stop after a healthy chunk —
                        # refill the budget so a long run rides cap after cap.
                        ext["reasserts_left"] = _EXTERNAL_OFF_MAX_REASSERTS
                    else:
                        ext["reasserts_left"] = reasserts_left - 1
                    # Next chunk is measured fresh from this re-assert.
                    ext["last_on_at"] = dt_util.utcnow()
                    ext["ran_healthy"] = False
                if decision == "retry_reset":
                    _LOGGER.warning(
                        "Zone %s stopped after a healthy run (controller cap-stop?); "
                        "re-asserting on to continue toward its full duration",
                        entity_id,
                    )
                else:
                    attempt = _EXTERNAL_OFF_MAX_REASSERTS - reasserts_left + 1
                    _LOGGER.warning(
                        "Zone %s went off mid-run (stale cloud state?); re-asserting on "
                        "(attempt %d/%d)",
                        entity_id,
                        attempt,
                        _EXTERNAL_OFF_MAX_REASSERTS,
                    )
                await hass.services.async_call(
                    "switch", SERVICE_TURN_ON, {"entity_id": entity_id}, blocking=False
                )
                return  # listener stays active; a further off re-arms the debounce

            # decision == "abort" — persistently off, out of attempts or past deadline.
            entry_data["manual_runs"].stop(entity_id)
            _clear_external_off(entry_data, entity_id)
            _cancel_handles(entry_data, entity_id)
            if coord is not None:
                rec = coord.run_history.abort_run(
                    entity_id,
                    ended_at=dt_util.utcnow(),
                    reason="switch turned off externally (did not recover)",
                )
                hass.async_create_task(coord.async_save_run_history())
                # Actionable "Run remainder" / "Open Logbook" alert for a
                # scheduled run cut short below 90% of planned.
                hass.async_create_task(_maybe_notify_cut_short(coord, rec))
            # v1.30 — a run that aborted because the switch won't stay on is in an
            # error state; don't auto-resume it on the next restart.
            _clear_active_session(hass, coord, entity_id)
            _LOGGER.warning(
                "Zone %s stayed off after %d re-assert(s) — aborting run",
                entity_id,
                _EXTERNAL_OFF_MAX_REASSERTS,
            )

        # ── state listener ──
        # A zone reporting off mid-run is debounced, not acted on immediately
        # (cloud integrations flap "off" for seconds after a turn-on). One
        # check is scheduled; further offs are ignored until it fires.
        @callback
        def _on_state_change(event):
            new_state = event.data.get("new_state")
            run_now = entry_data["manual_runs"].get(entity_id)
            if run_now is None:
                return  # our run already ended
            ext = entry_data.setdefault("external_off", {}).setdefault(
                entity_id,
                {
                    "reasserts_left": _EXTERNAL_OFF_MAX_REASSERTS,
                    "pending": None,
                    "last_on_at": dt_util.utcnow(),
                    "ran_healthy": False,
                },
            )
            if new_state is None:
                return
            if new_state.state == "on":
                # Zone (re)started — measure the next chunk from here.
                ext["last_on_at"] = dt_util.utcnow()
                return
            # Off transition: was the chunk it just finished a healthy run
            # (a controller cap-stop) or a quick flap/fault? Drives whether
            # the re-assert budget gets refilled (see external_off_decision).
            on_seconds = (
                dt_util.utcnow() - ext.get("last_on_at", dt_util.utcnow())
            ).total_seconds()
            ext["ran_healthy"] = on_seconds >= _EXTERNAL_OFF_HEALTHY_RUN_SECONDS
            if ext.get("pending") is not None:
                return  # a debounce check is already scheduled
            ext["pending"] = async_call_later(
                hass, _EXTERNAL_OFF_DEBOUNCE_SECONDS, _verify_external_off
            )
            _LOGGER.debug(
                "Zone %s reported off mid-run after %.0fs on; debouncing %ds before reacting",
                entity_id,
                on_seconds,
                _EXTERNAL_OFF_DEBOUNCE_SECONDS,
            )

        cancel_listener = async_track_state_change_event(hass, [entity_id], _on_state_change)

        entry_data.setdefault("external_off", {})[entity_id] = {
            "reasserts_left": _EXTERNAL_OFF_MAX_REASSERTS,
            "pending": None,
            "last_on_at": dt_util.utcnow(),
            "ran_healthy": False,
        }
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
        if not await _require_admin_if_configured(hass, call):
            return
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
        _clear_external_off(entry_data, entity_id)
        _cancel_handles(entry_data, entity_id)
        # v1.25 — cancel any pending chunked-run blocks so Stop truly stops the
        # run; otherwise later blocks would re-open the valve after the user
        # turned it off.
        _cancel_block_timers(entry_data, entity_id)
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
        # v1.30 — a Stop ends the whole run (incl. a chunked session), so drop
        # the restart-resume session: a later restart must not revive it.
        _clear_active_session(hass, coord, entity_id)
        _LOGGER.info("Stopped manual run for %s by request", entity_id)

    # ── Schedule CRUD ──────────────────────────────────────────────

    async def handle_add_schedule(call: ServiceCall) -> None:
        if not await _require_admin(hass, call):  # config write — admin only
            return
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
            ignore_wind=data.get("ignore_wind", False),
            ignore_hot_weather=data.get("ignore_hot_weather", False),
            ignore_rain_lockout=data.get("ignore_rain_lockout", False),
            color=data.get("color"),
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
        if not await _require_admin(hass, call):  # config write — admin only
            return
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
            "ignore_wind",
            "ignore_hot_weather",
            "ignore_rain_lockout",
            "color",
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
        if not await _require_admin(hass, call):  # destructive — admin only
            return
        data = _DELETE_SCHEDULE_SCHEMA(dict(call.data))
        coord = _find_coordinator(hass)
        if coord is None:
            return
        # Resolve the schedule's zone(s) BEFORE deleting so we can stop any
        # chunked-run blocks in flight for them (v1.25).
        sched = coord.schedule_store.get(data["schedule_id"])
        if coord.schedule_store.delete(data["schedule_id"]):
            await coord.async_save()
            if sched is not None:
                for step in sched.all_steps():
                    ed = _find_entry_data(hass, step.zone_entity_id)
                    if ed is not None:
                        _cancel_block_timers(ed, step.zone_entity_id)
            _LOGGER.info("Deleted schedule %s", data["schedule_id"])

    async def handle_run_schedule(call: ServiceCall) -> None:
        """v1.17.13 — execute a schedule's full run sequence on demand.

        Powered by the "Run" button on each schedule row in the panel.
        Bypasses moisture / wind / hot-weather / rain-lockout gates
        (manual override semantics: the user clicked Run, they meant
        it). Conflict resolver is NOT consulted since this is a
        user-initiated immediate action — the underlying run_zone
        service replaces any in-progress run on the same zone.

        Multi-zone schedules fire zone-by-zone with the user's
        configured inter-zone buffer (zone_buffer_seconds, default 30s)
        between steps. Each step's auto-stop timer in handle_run_zone
        cuts that zone before the next one starts.
        """
        if not await _require_admin_if_configured(hass, call):
            return
        data = _RUN_SCHEDULE_SCHEMA(dict(call.data))
        coord = _find_coordinator(hass)
        if coord is None:
            return
        sched = coord.schedule_store.get(data["schedule_id"])
        if sched is None:
            _LOGGER.warning("run_schedule: no such schedule %s", data["schedule_id"])
            return

        buffer_s = int(coord.config.get("zone_buffer_seconds", 30))
        steps = sched.all_steps()
        cumulative_offset = 0

        for step in steps:
            entity_id = step.zone_entity_id
            minutes = int(step.duration_minutes)
            if cumulative_offset == 0:
                # Fire the first step immediately
                await hass.services.async_call(
                    DOMAIN,
                    SERVICE_RUN_ZONE,
                    {"entity_id": entity_id, "minutes": minutes},
                    blocking=False,
                )
            else:
                # Schedule subsequent steps via async_call_later.
                # Default-arg pattern captures eid/mins by value (not by
                # reference) so each closure fires its own step.
                async def _fire_step(_now=None, eid=entity_id, mins=minutes):
                    await hass.services.async_call(
                        DOMAIN,
                        SERVICE_RUN_ZONE,
                        {"entity_id": eid, "minutes": mins},
                        blocking=False,
                    )

                async_call_later(hass, cumulative_offset, _fire_step)
            cumulative_offset += minutes * 60 + buffer_s

        total_min = (cumulative_offset - buffer_s) // 60
        _LOGGER.info(
            "Run schedule %s (%s) on demand: %d zone(s) over ~%d min",
            sched.id,
            sched.name,
            len(steps),
            total_min,
        )

    async def handle_set_schedule_enabled(call: ServiceCall) -> None:
        if not await _require_admin_if_configured(hass, call):
            return
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

    # ── v2: plant CRUD (plant-aware irrigation) ────────────────────

    async def handle_add_plant(call: ServiceCall) -> None:
        if not await _require_admin(hass, call):  # config write — admin only
            return
        data = _ADD_PLANT_SCHEMA(dict(call.data))
        coord = _find_coordinator(hass)
        if coord is None:
            _LOGGER.warning("add_plant called but no coordinator available")
            return
        plant = PlantRecord(
            id=uuid.uuid4().hex[:12],
            name=data["name"],
            wucols_category=data["wucols_category"],
            canopy_area_sqft=data["canopy_area_sqft"],
            zone_entity_id=data["zone_entity_id"],
            species=data.get("species", ""),
        )
        coord.plants.add(plant)
        await coord.async_save_plants()
        _LOGGER.info("Added plant %s (%s) on %s", plant.id, plant.name, plant.zone_entity_id)

    async def handle_update_plant(call: ServiceCall) -> None:
        if not await _require_admin(hass, call):  # config write — admin only
            return
        data = _UPDATE_PLANT_SCHEMA(dict(call.data))
        coord = _find_coordinator(hass)
        if coord is None:
            return
        existing = coord.plants.get(data["plant_id"])
        if existing is None:
            _LOGGER.warning("update_plant: no such plant %s", data["plant_id"])
            return
        overrides = {
            k: data[k]
            for k in (
                "name",
                "wucols_category",
                "canopy_area_sqft",
                "zone_entity_id",
                "map_x",
                "map_y",
                "species",
            )
            if k in data
        }
        # replace() re-runs PlantRecord validation on the merged record.
        merged = replace(existing, **overrides)
        coord.plants.upsert(merged)
        await coord.async_save_plants()
        _LOGGER.info("Updated plant %s", merged.id)

    async def handle_add_plant_photo(call: ServiceCall) -> None:
        if not await _require_admin(hass, call):  # writes file bytes — admin only
            return
        data = _ADD_PLANT_PHOTO_SCHEMA(dict(call.data))
        coord = _find_coordinator(hass)
        if coord is None:
            return
        existing = coord.plants.get(data["plant_id"])
        if existing is None:
            _LOGGER.warning("add_plant_photo: no such plant %s", data["plant_id"])
            return

        import base64
        import binascii
        import os

        from homeassistant.util import dt as dt_util

        raw_b64 = data["image_base64"]
        if "," in raw_b64[:64]:  # tolerate a data:image/...;base64, prefix
            raw_b64 = raw_b64.split(",", 1)[1]
        try:
            image = base64.b64decode(raw_b64, validate=True)
        except (binascii.Error, ValueError):
            _LOGGER.warning("add_plant_photo: image_base64 is not valid base64")
            return
        if len(image) < 100:
            _LOGGER.warning("add_plant_photo: decoded image too small")
            return
        if not _looks_like_image(image):
            # Reject non-images so a crafted SVG/HTML can't be stored as .jpg and
            # served from /local (content-sniffing / stored-XSS vector).
            _LOGGER.warning("add_plant_photo: payload is not a recognized image")
            return

        ts = int(dt_util.utcnow().timestamp())
        plants_root = hass.config.path("www", "complete_irrigation", "plants")
        abs_dir = os.path.join(plants_root, existing.id)
        fname = f"{ts}.jpg"
        abs_path = os.path.join(abs_dir, fname)
        # Defense-in-depth at the filesystem sink: the id is charset-validated on
        # construct/load, but verify the resolved write path can't escape the
        # plants dir even if that guard were ever bypassed (path traversal).
        if os.path.commonpath(
            [os.path.realpath(abs_dir), os.path.realpath(plants_root)]
        ) != os.path.realpath(plants_root):
            _LOGGER.error("add_plant_photo: refusing unsafe path for id %r", existing.id)
            return

        def _save() -> None:
            os.makedirs(abs_dir, exist_ok=True)
            with open(abs_path, "wb") as fh:
                fh.write(image)

        try:
            await hass.async_add_executor_job(_save)
        except OSError as err:
            _LOGGER.warning("add_plant_photo: could not write image: %s", err)
            return

        url_path = f"/local/complete_irrigation/plants/{existing.id}/{fname}"
        photo = {"ts": ts, "path": url_path, "note": data.get("note", "")}
        # newest-first, capped.
        photos = (photo, *existing.photos)[:_MAX_PHOTOS_PER_PLANT]
        overrides: dict[str, Any] = {"photos": photos}

        # EXIF-GPS auto-place — ONLY if the plant is still unplaced AND we have GPS
        # AND a yard map exists. GPS places ONCE; the plant_id (selection) owns
        # identity on every update, manual drag owns position (the locked workflow).
        if existing.map_x is None and "latitude" in data and "longitude" in data:
            from .yard_map import norm_from_yard_map

            xy = norm_from_yard_map(
                data["latitude"], data["longitude"], coord.config.get("yard_map")
            )
            if xy is not None:
                overrides["map_x"], overrides["map_y"] = xy
                _LOGGER.info("add_plant_photo: auto-placed %s from photo GPS", existing.id)

        merged = replace(existing, **overrides)
        coord.plants.upsert(merged)
        await coord.async_save_plants()
        _LOGGER.info("add_plant_photo: %s now has %d photo(s)", existing.id, len(merged.photos))

    async def handle_set_plant_health(call: ServiceCall) -> None:
        # v1.33 — the NAS vision job posts its verdict; the vision_health rail bounds
        # it (strips actuation, clamps, caps) before we store it on the plant. Admin
        # only (writes stored state + can fire a concern notification).
        if not await _require_admin(hass, call):
            return
        data = _SET_PLANT_HEALTH_SCHEMA(dict(call.data))
        coord = _find_coordinator(hass)
        if coord is None:
            return
        existing = coord.plants.get(data["plant_id"])
        if existing is None:
            _LOGGER.warning("set_plant_health: no such plant %s", data["plant_id"])
            return

        from homeassistant.util import dt as dt_util

        from .vision_health import (
            report_has_concern,
            select_review_photos,
            serialize_report,
            validate_verdict,
        )

        sel = select_review_photos(list(existing.photos))
        latest_ts = sel["latest"]["ts"] if sel else None
        baseline_ts = sel["baseline"]["ts"] if (sel and sel["baseline"]) else None
        report = validate_verdict(
            data["verdict"],
            plant_id=existing.id,
            latest_ts=latest_ts,
            baseline_ts=baseline_ts,
            model=data.get("model", ""),
            now_iso=dt_util.utcnow().isoformat(),
        )
        if report is None:
            _LOGGER.warning("set_plant_health: unusable verdict for %s", existing.id)
            return

        merged = replace(existing, health=serialize_report(report))
        coord.plants.upsert(merged)
        await coord.async_save_plants()
        _LOGGER.info(
            "set_plant_health: %s -> %s (confidence %.2f)",
            existing.id,
            report.health_state,
            report.confidence,
        )
        # Surface a concern to the user (respects the notifier's enabled/quiet-hours).
        if report_has_concern(report) and coord.notifier is not None:
            concerns = "; ".join(report.concerns) if report.concerns else report.health_state
            await coord.notifier.notify(
                f"{existing.name}: {concerns}",
                title="Plant health check",
                event_type="plant_health",
                extra={"plant_id": existing.id, "health_state": report.health_state},
            )

    async def handle_delete_plant(call: ServiceCall) -> None:
        if not await _require_admin(hass, call):  # destructive — admin only
            return
        data = _DELETE_PLANT_SCHEMA(dict(call.data))
        coord = _find_coordinator(hass)
        if coord is None:
            return
        if coord.plants.delete(data["plant_id"]):
            coord.cancel_light_survey(data["plant_id"])  # v1.35 — drop any live survey
            await coord.async_save_plants()
            # v1.35 — cascade: a care task for a deleted plant would stay
            # permanently "due" and can't be retargeted; remove them too.
            orphaned = coord.care_tasks.for_plant(data["plant_id"])
            if orphaned:
                for t in orphaned:
                    coord.care_tasks.delete(t.id)
                await coord.async_save_care_tasks()
                _LOGGER.info(
                    "Deleted %d care task(s) attached to plant %s",
                    len(orphaned),
                    data["plant_id"],
                )
            _LOGGER.info("Deleted plant %s", data["plant_id"])

    # ── v1.35: light surveys ────────────────────────────────────────

    async def handle_set_plant_light_range(call: ServiceCall) -> None:
        if not await _require_admin(hass, call):  # config write — admin only
            return
        data = _SET_PLANT_LIGHT_RANGE_SCHEMA(dict(call.data))
        coord = _find_coordinator(hass)
        if coord is None:
            return
        existing = coord.plants.get(data["plant_id"])
        if existing is None:
            _LOGGER.warning("set_plant_light_range: no such plant %s", data["plant_id"])
            return
        if data.get("clear"):
            low = high = None
        else:
            if "lux_low" not in data or "lux_high" not in data:
                raise vol.Invalid("provide both lux_low and lux_high, or clear=true")
            try:
                low, high = validate_lux_range(data["lux_low"], data["lux_high"])
            except ValueError as err:
                # Surface the rail's message as a proper validation error, not an
                # unknown_error traceback in the caller's face.
                raise vol.Invalid(str(err)) from err
        merged = replace(existing, lux_low=low, lux_high=high)
        coord.plants.upsert(merged)
        await coord.async_save_plants()
        _LOGGER.info("Set light range for %s: %s..%s lux", merged.id, low, high)

    async def handle_start_light_survey(call: ServiceCall) -> None:
        if not await _require_admin(hass, call):  # drives sampling — admin only
            return
        data = _START_LIGHT_SURVEY_SCHEMA(dict(call.data))
        coord = _find_coordinator(hass)
        if coord is None:
            return
        plant = coord.plants.get(data["plant_id"])
        if plant is None:
            _LOGGER.warning("start_light_survey: no such plant %s", data["plant_id"])
            return
        sensor = data["sensor_entity_id"]
        if hass.states.get(sensor) is None:
            raise vol.Invalid(f"sensor {sensor} does not exist")
        coord.start_light_survey(plant.id, sensor, data["minutes"])
        _LOGGER.info(
            "Light survey started for %s (%s) on %s for %d min",
            plant.name,
            plant.id,
            sensor,
            data["minutes"],
        )

    async def handle_cancel_light_survey(call: ServiceCall) -> None:
        if not await _require_admin(hass, call):
            return
        data = _CANCEL_LIGHT_SURVEY_SCHEMA(dict(call.data))
        coord = _find_coordinator(hass)
        if coord is None:
            return
        if coord.cancel_light_survey(data["plant_id"]):
            _LOGGER.info("Light survey cancelled for %s", data["plant_id"])

    # ── v1.35: care-task reminders ──────────────────────────────────

    async def handle_add_care_task(call: ServiceCall) -> None:
        if not await _require_admin(hass, call):  # config write — admin only
            return
        data = _ADD_CARE_TASK_SCHEMA(dict(call.data))
        coord = _find_coordinator(hass)
        if coord is None:
            return
        if data["plant_id"] and coord.plants.get(data["plant_id"]) is None:
            raise vol.Invalid(f"no such plant {data['plant_id']}")
        if data["zone_entity_id"] and hass.states.get(data["zone_entity_id"]) is None:
            raise vol.Invalid(f"no such zone entity {data['zone_entity_id']}")
        # CareTask.__post_init__ enforces plant-or-zone + custom-needs-label —
        # surfaced as a proper validation error, not an unknown_error traceback.
        try:
            task = CareTask(
                id=uuid.uuid4().hex[:12],
                kind=data["kind"],
                interval_days=data["interval_days"],
                plant_id=data["plant_id"],
                zone_entity_id=data["zone_entity_id"],
                label=data["label"],
            )
        except ValueError as err:
            raise vol.Invalid(str(err)) from err
        coord.care_tasks.add(task)
        await coord.async_save_care_tasks()
        _LOGGER.info("Added care task %s (%s every %dd)", task.id, task.kind, task.interval_days)

    async def handle_update_care_task(call: ServiceCall) -> None:
        if not await _require_admin(hass, call):
            return
        data = _UPDATE_CARE_TASK_SCHEMA(dict(call.data))
        coord = _find_coordinator(hass)
        if coord is None:
            return
        existing = coord.care_tasks.get(data["task_id"])
        if existing is None:
            _LOGGER.warning("update_care_task: no such task %s", data["task_id"])
            return
        overrides = {k: data[k] for k in ("kind", "interval_days", "label", "enabled") if k in data}
        try:
            merged = replace(existing, **overrides)
        except ValueError as err:
            # e.g. switching kind to custom without a label — a validation error.
            raise vol.Invalid(str(err)) from err
        coord.care_tasks.upsert(merged)
        await coord.async_save_care_tasks()
        _LOGGER.info("Updated care task %s", merged.id)

    async def handle_seed_care_plan(call: ServiceCall) -> None:
        if not await _require_admin(hass, call):  # config write — admin only
            return
        data = _SEED_CARE_PLAN_SCHEMA(dict(call.data))
        coord = _find_coordinator(hass)
        if coord is None:
            return
        plant = coord.plants.get(data["plant_id"])
        if plant is None:
            raise vol.Invalid(f"no such plant {data['plant_id']}")
        tasks = seed_care_plan(plant.id, data["preset"], id_factory=lambda: uuid.uuid4().hex[:12])
        # Idempotent: skip kinds the plant already has a task for.
        existing_kinds = {t.kind for t in coord.care_tasks.for_plant(plant.id)}
        added = 0
        for t in tasks:
            if t.kind in existing_kinds:
                continue
            coord.care_tasks.add(t)
            added += 1
        if added:
            await coord.async_save_care_tasks()
        _LOGGER.info(
            "Seeded care plan '%s' for %s: %d task(s) added, %d already present",
            data["preset"],
            plant.name,
            added,
            len(tasks) - added,
        )

    async def handle_delete_care_task(call: ServiceCall) -> None:
        if not await _require_admin(hass, call):  # destructive — admin only
            return
        data = _DELETE_CARE_TASK_SCHEMA(dict(call.data))
        coord = _find_coordinator(hass)
        if coord is None:
            return
        if coord.care_tasks.delete(data["task_id"]):
            await coord.async_save_care_tasks()
            _LOGGER.info("Deleted care task %s", data["task_id"])

    async def handle_complete_care_task(call: ServiceCall) -> None:
        if not await _require_admin(hass, call):
            return
        data = _COMPLETE_CARE_TASK_SCHEMA(dict(call.data))
        coord = _find_coordinator(hass)
        if coord is None:
            return
        existing = coord.care_tasks.get(data["task_id"])
        if existing is None:
            _LOGGER.warning("complete_care_task: no such task %s", data["task_id"])
            return
        from homeassistant.util import dt as dt_util

        coord.care_tasks.upsert(existing.completed(int(dt_util.now().timestamp())))
        await coord.async_save_care_tasks()
        _LOGGER.info("Care task %s marked done", existing.id)

    async def handle_set_yard_map(call: ServiceCall) -> None:
        if not await _require_admin(hass, call):  # writes file + outbound fetch — admin only
            return
        data = _SET_YARD_MAP_SCHEMA(dict(call.data))
        coord = _find_coordinator(hass)
        if coord is None:
            return
        import os

        import aiohttp
        from homeassistant.helpers.aiohttp_client import async_get_clientsession
        from homeassistant.util import dt as dt_util

        from .yard_map import (
            bbox_from_center,
            esri_export_url,
            image_size_for_bbox,
            safe_export_size,
        )

        lat = data.get("latitude", hass.config.latitude)
        lon = data.get("longitude", hass.config.longitude)
        span = float(data.get("span_m", 60.0))
        if lat is None or lon is None:
            _LOGGER.warning("set_yard_map: no location — set HA latitude/longitude or pass them")
            return

        bbox = bbox_from_center(float(lat), float(lon), span)
        width, height = image_size_for_bbox(bbox)
        # Esri's export now HTTP-500s on requests sharper than its imagery cache
        # (~0.3 m/px) — fetch at the sharpest scale it will serve, then upscale
        # locally back to the display size (same bbox, marker math untouched).
        fetch_w, fetch_h = safe_export_size(span, width, height)
        url = esri_export_url(bbox, fetch_w, fetch_h)

        session = async_get_clientsession(hass)
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    _LOGGER.warning("set_yard_map: aerial service returned HTTP %s", resp.status)
                    return
                content = await resp.read()
        except Exception as err:  # network / timeout / client errors — never crash
            _LOGGER.warning("set_yard_map: aerial fetch failed: %s", err)
            return
        if not content or len(content) < 1024:
            _LOGGER.warning("set_yard_map: aerial fetch returned too little data")
            return
        if not _looks_like_image(content):
            # The aerial host returns image bytes; an error page (HTML, HTTP 200)
            # must not be saved as yard_map.jpg and served from /local.
            _LOGGER.warning("set_yard_map: aerial fetch did not return an image")
            return

        abs_dir = hass.config.path("www", "complete_irrigation")
        abs_path = os.path.join(abs_dir, "yard_map.jpg")

        def _save() -> None:
            os.makedirs(abs_dir, exist_ok=True)
            payload = content
            if (fetch_w, fetch_h) != (width, height):
                # Fetched coarser than display size (Esri cache-scale floor) —
                # upscale for crisp rendering. Pillow ships with HA core; if it's
                # somehow unavailable, save the small image (still fully usable).
                try:
                    import io

                    from PIL import Image

                    img = Image.open(io.BytesIO(content))
                    img = img.convert("RGB").resize((width, height), Image.LANCZOS)
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=88)
                    payload = buf.getvalue()
                except Exception as err:  # cosmetic step — never fatal
                    _LOGGER.warning("set_yard_map: upscale skipped (%s)", err)
            with open(abs_path, "wb") as fh:
                fh.write(payload)

        try:
            await hass.async_add_executor_job(_save)
        except OSError as err:
            _LOGGER.warning("set_yard_map: could not write image: %s", err)
            return

        version = dt_util.utcnow().strftime("%Y%m%d%H%M%S")
        coord.config["yard_map"] = {
            "center_lat": float(lat),
            "center_lon": float(lon),
            "span_m": span,
            "bbox": {
                "west": bbox.west,
                "south": bbox.south,
                "east": bbox.east,
                "north": bbox.north,
            },
            "width": width,
            "height": height,
            "image_path": f"/local/complete_irrigation/yard_map.jpg?v={version}",
            "version": version,
        }
        await coord.async_save_config()
        _LOGGER.info(
            "Yard map updated (%dx%d) centered %.5f,%.5f span %.0fm", width, height, lat, lon, span
        )

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
    hass.services.async_register(
        DOMAIN,
        SERVICE_RUN_SCHEDULE,
        handle_run_schedule,
        schema=_RUN_SCHEDULE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_ADD_PLANT, handle_add_plant, schema=_ADD_PLANT_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_UPDATE_PLANT, handle_update_plant, schema=_UPDATE_PLANT_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_DELETE_PLANT, handle_delete_plant, schema=_DELETE_PLANT_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_YARD_MAP, handle_set_yard_map, schema=_SET_YARD_MAP_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_ADD_PLANT_PHOTO, handle_add_plant_photo, schema=_ADD_PLANT_PHOTO_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_PLANT_HEALTH, handle_set_plant_health, schema=_SET_PLANT_HEALTH_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_PLANT_LIGHT_RANGE,
        handle_set_plant_light_range,
        schema=_SET_PLANT_LIGHT_RANGE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_START_LIGHT_SURVEY,
        handle_start_light_survey,
        schema=_START_LIGHT_SURVEY_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CANCEL_LIGHT_SURVEY,
        handle_cancel_light_survey,
        schema=_CANCEL_LIGHT_SURVEY_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_ADD_CARE_TASK, handle_add_care_task, schema=_ADD_CARE_TASK_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_UPDATE_CARE_TASK, handle_update_care_task, schema=_UPDATE_CARE_TASK_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_DELETE_CARE_TASK, handle_delete_care_task, schema=_DELETE_CARE_TASK_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_COMPLETE_CARE_TASK,
        handle_complete_care_task,
        schema=_COMPLETE_CARE_TASK_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SEED_CARE_PLAN, handle_seed_care_plan, schema=_SEED_CARE_PLAN_SCHEMA
    )

    # ── Weather + per-zone moisture configuration ──────────────────

    async def handle_set_weather_config(call: ServiceCall) -> None:
        if not await _require_admin(hass, call):  # config write — admin only
            return
        data = _SET_WEATHER_CONFIG_SCHEMA(dict(call.data))
        coord = _find_coordinator(hass)
        if coord is None:
            return
        eto_was_auto = bool(coord.config.get("eto_auto"))
        for key in (
            "rain_sensor",
            "rain_sensors",
            "temperature_sensor",
            "hot_threshold_f",
            "boost_percent",
            "wind_sensor",
            "wind_defer_mph",
            "eto_in_week",
            "eto_auto",
            "weather_entity",
            "drip_efficiency",
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
        # v1.28 — if auto ETo just turned on (or the weather entity changed
        # while it's on), fetch a fresh figure now rather than waiting for the
        # daily 03:00 refresh. Await it (don't fire-and-forget) so the service
        # only returns once the new value is stored — the Yard UI's refetch
        # right after this call then sees the fresh figure with no timing race.
        if coord.config.get("eto_auto") and (not eto_was_auto or "weather_entity" in data):
            await coord._refresh_auto_eto()
        _LOGGER.info("Weather config updated: %s", data)

    async def handle_set_zone_moisture(call: ServiceCall) -> None:
        if not await _require_admin_if_configured(hass, call):
            return
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
        if not await _require_admin_if_configured(hass, call):
            return
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
        if not await _require_admin(hass, call):  # config write — admin only
            return
        data = _SET_NOTIFICATION_CONFIG_SCHEMA(dict(call.data))
        coord = _find_coordinator(hass)
        if coord is None or coord.notifier is None:
            return
        coord.notifier.update_config(**data)
        coord.config.setdefault("notifications", {}).update(data)
        await coord.async_save_config()
        _LOGGER.info("Notification config updated: %s", data)

    async def handle_test_notification(call: ServiceCall) -> None:
        if not await _require_admin_if_configured(hass, call):
            return
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
        if not await _require_admin(hass, call):  # config write — admin only
            return
        data = _SET_CONFLICT_POLICY_SCHEMA(dict(call.data))
        coord = _find_coordinator(hass)
        if coord is None:
            return
        coord.config["conflict_policy"] = data["policy"]
        await coord.async_save_config()
        _LOGGER.info("Conflict policy updated: %s", data["policy"])

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_CONFLICT_POLICY,
        handle_set_conflict_policy,
        schema=_SET_CONFLICT_POLICY_SCHEMA,
    )

    async def handle_set_general_config(call: ServiceCall) -> None:
        """PRD #38 + #81 — generic bucket for one-off global settings.

        Each entry in _GENERAL_CONFIG_FIELDS describes how to persist
        one accepted key. v1.16: loop-driven so a new field is a 2-line
        table edit instead of another `if` arm."""
        if not await _require_admin(hass, call):  # config write — admin only
            return
        data = _SET_GENERAL_CONFIG_SCHEMA(dict(call.data))
        coord = _find_coordinator(hass)
        if coord is None:
            return
        for key, transform in _GENERAL_CONFIG_FIELDS.items():
            if key in data:
                coord.config[key] = transform(data[key])
        await coord.async_save_config()
        _LOGGER.info("General config updated: %s", data)

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_GENERAL_CONFIG,
        handle_set_general_config,
        schema=_SET_GENERAL_CONFIG_SCHEMA,
    )

    async def handle_start_establishment(call: ServiceCall) -> None:
        if not await _require_admin(hass, call):  # config write — admin only
            return
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
        if not await _require_admin(hass, _call):  # destructive — admin only
            return
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
        SERVICE_RUN_SCHEDULE,
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
        SERVICE_SET_CONFLICT_POLICY,
        SERVICE_SET_GENERAL_CONFIG,
        SERVICE_ADD_PLANT,
        SERVICE_UPDATE_PLANT,
        SERVICE_DELETE_PLANT,
        SERVICE_SET_YARD_MAP,
        SERVICE_ADD_PLANT_PHOTO,
        SERVICE_SET_PLANT_HEALTH,
        SERVICE_SET_PLANT_LIGHT_RANGE,
        SERVICE_START_LIGHT_SURVEY,
        SERVICE_CANCEL_LIGHT_SURVEY,
        SERVICE_ADD_CARE_TASK,
        SERVICE_UPDATE_CARE_TASK,
        SERVICE_DELETE_CARE_TASK,
        SERVICE_COMPLETE_CARE_TASK,
        SERVICE_SEED_CARE_PLAN,
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
    # v1.21 — also cancel any pending external-off debounce timers.
    external_off = entry_data.get("external_off", {})
    for ext in list(external_off.values()):
        if ext.get("pending"):
            ext["pending"]()
    external_off.clear()
    # v1.25 — cancel any pending chunked-run block timers so a stale block can't
    # fire run_zone after the entry is torn down (unload / reload).
    block_timers = entry_data.get("block_timers", {})
    for cancels in list(block_timers.values()):
        for cancel in cancels:
            try:
                cancel()
            except Exception:
                _LOGGER.debug("block-timer cancel during cleanup was already fired")
    block_timers.clear()
    entry_data.get("block_seq", {}).clear()
