"""Schedule coordinator — loads persisted schedules + fires due runs on a tick.

Per-entry orchestrator. Each tick:
  1. Compute planned runs (next_runs)
  2. Resolve conflicts (resolve_conflicts, default policy = DEFER_NEW)
  3. Check rain lockout (skip a zone type while its hold is active)
  4. For each due run:
       a. Read per-zone moisture sensors (if configured) → MoistureGate
       b. Read forecast → hot-weather boost multiplier
       c. Fire via complete_irrigation.run_zone with adjusted duration,
          or skip with logged reason
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Any

from .care_tasks import CareTaskStore
from .chunked_run import DEFAULT_CONTROLLER_MAX_RUN_MIN
from .conflict_resolver import (
    DEFAULT_CASCADE_CAP_MINUTES,
    POLICY_DEFER_NEW,
    POLICY_SHIFT_EXISTING_EARLIER,
    POLICY_SPLIT_DIFFERENCE,
    REASON_COMMITTED,
    REASON_DEFERRED_LATE,
    committed_runs_from_sessions,
    resolve_conflicts,
    stale_session_zones,
)
from .const import (
    DEFAULT_BUFFER_MINUTES,
    DEFAULT_ZONE_MAX_PCT,
    DEFAULT_ZONE_MIN_PCT,
    DEFAULT_ZONE_TARGET_PCT,
    DOMAIN,
    MAX_SCHEDULE_DURATION_MIN,
)

# v1.16 — pure-logic helpers live in coordinator_logic.py now; re-exported
# here so every existing `from .coordinator import build_weekly_zone_summary`
# (and friends) keeps resolving.
from .coordinator_logic import (
    build_weekly_zone_summary,
    compute_expired_establishment_schedules,
    compute_low_moisture_offenders,
    detect_sensor_offline_transitions,
    plan_session_resume,
)
from .et_calc import SANE_ETO_MAX, select_eto, weekly_eto_inches_from_forecast
from .gap_insertion import REASON_GAP_INSERTED, insertion_key, plan_gap_insertions
from .hydraulics import DEFAULT_ETO_IN_WEEK
from .light_survey import (
    MAX_SURVEYS_KEPT as _MAX_SURVEYS_KEPT,
)
from .light_survey import (
    light_verdict,
    make_survey_entry,
    summarize_samples,
    verdict_text,
)
from .moisture_gate import COMBINE_AVERAGE, combine_moisture, evaluate_moisture
from .notifications import (
    CATEGORY_IMPORTANT,
    CATEGORY_INFORMATIONAL,
    NotificationDispatcher,
    _parse_hhmm,
)
from .plant import PlantStore
from .rain_lockout import (
    DURABLE_ZONE_TYPES,
    RAIN_SOURCE_DISAGREE,
    SAME_EVENT_EPSILON_IN,
    TYPE_BIRDBATH,
    TYPE_GERMINATION,
    CombinedRain,
    LockoutState,
    active_rain,
    classify_zone_type,
    combine_rain_totals,
    moisture_releases_lockout,
    parse_open_meteo_rain,
    scaled_lockout_hours,
    select_lockout_daily_eto,
    sum_daily_peaks,
)
from .run_history import (
    DEFAULT_RETENTION_DAYS,
    RunHistoryStore,
)
from .run_planner import due_runs_since, longest_run_span_minutes, next_runs
from .schedule import ScheduleStore
from .weather_gate import (
    evaluate_hot_weather,
    evaluate_wind_defer,
    rain_to_inches,
    temp_to_fahrenheit,
)
from .zone_region import ZoneRegionStore

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


def _fmt_in(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _plain_float(value: object) -> float | None:
    """A real number. Rejects bools and test doubles (MagicMock floats as 1.0)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return float(value)


_VALID_CONFLICT_POLICIES = {
    POLICY_DEFER_NEW,
    POLICY_SHIFT_EXISTING_EARLIER,
    POLICY_SPLIT_DIFFERENCE,
}

STORAGE_VERSION = 1
TICK_SECONDS = 60

# v1.17 — restart-missed lookback window. On startup, if the persisted
# last_tick is at most this many minutes old, the coordinator resumes
# from there so a fast HA restart spanning a fire-minute still catches
# the run. Longer gaps fall back to "now" — long outages would otherwise
# burst-fire every missed run, which floods the irrigation system.
_MISSED_LOOKBACK_MIN = 5
# v1.30 — restart fail-over: how long after startup to wait before resuming an
# interrupted run, so switch integrations (Rachio etc.) have time to come back
# online before we re-assert the valve. If a zone's switch still isn't available,
# retry up to _RESUME_MAX_ATTEMPTS times spaced _RESUME_RETRY_SECONDS apart rather
# than dropping the run (a slow cloud reconnect must not lose a giant's water).
_RESUME_DELAY_SECONDS = 30
_RESUME_RETRY_SECONDS = 60
_RESUME_MAX_ATTEMPTS = 5

# v1.19 — auto-soak defaults + give-up cooldown. After soak_max_cycles
# fail to raise moisture above min_pct, the zone won't re-attempt for
# this long (and the user is notified) — the brake that stops a
# stuck-low sensor from watering all day.
_SOAK_DEFAULT_RUN_MIN = 10
_SOAK_DEFAULT_WAIT_MIN = 30
_SOAK_DEFAULT_MAX_CYCLES = 4
_SOAK_GIVE_UP_COOLDOWN_MIN = 360

# v1.17 — Companion-notification action prefix. Used for both sending
# the action and matching incoming mobile_app_notification_action events.
RUN_MISSED_ACTION = "COMPLETE_IRRIGATION_RUN_MISSED"
# v1.17.8 — fired when the user taps "Run remainder (N min)" on a
# cut-short notification (see services.py _maybe_notify_cut_short).
RUN_REMAINDER_ACTION = "COMPLETE_IRRIGATION_RUN_REMAINDER"


def _storage_key(entry_id: str, suffix: str = "schedules") -> str:
    return f"{DOMAIN}.{suffix}.{entry_id}"


async def _async_load_schedules(hass: HomeAssistant, entry_id: str) -> ScheduleStore:
    from homeassistant.helpers.storage import Store

    helper = Store(hass, STORAGE_VERSION, _storage_key(entry_id))
    data = await helper.async_load()
    if not data:
        return ScheduleStore()
    return ScheduleStore.from_serializable(data.get("schedules", []))


async def _async_save_schedules(hass: HomeAssistant, entry_id: str, store: ScheduleStore) -> None:
    from homeassistant.helpers.storage import Store

    helper = Store(hass, STORAGE_VERSION, _storage_key(entry_id))
    await helper.async_save({"schedules": store.to_serializable()})


async def _async_load_config(hass: HomeAssistant, entry_id: str) -> dict[str, Any]:
    """Load coordinator config (weather + zone moisture)."""
    from homeassistant.helpers.storage import Store

    helper = Store(hass, STORAGE_VERSION, _storage_key(entry_id, "config"))
    data = await helper.async_load()
    return data or {}


async def _async_save_config(hass: HomeAssistant, entry_id: str, config: dict[str, Any]) -> None:
    from homeassistant.helpers.storage import Store

    helper = Store(hass, STORAGE_VERSION, _storage_key(entry_id, "config"))
    await helper.async_save(config)


async def _async_load_run_history(hass: HomeAssistant, entry_id: str) -> RunHistoryStore:
    from homeassistant.helpers.storage import Store

    helper = Store(hass, STORAGE_VERSION, _storage_key(entry_id, "run_history"))
    data = await helper.async_load()
    if not data:
        return RunHistoryStore()
    return RunHistoryStore.from_serializable(data.get("records", []))


async def _async_save_run_history(
    hass: HomeAssistant, entry_id: str, store: RunHistoryStore
) -> None:
    from homeassistant.helpers.storage import Store

    helper = Store(hass, STORAGE_VERSION, _storage_key(entry_id, "run_history"))
    await helper.async_save({"records": store.to_serializable()})


async def _async_load_plants(hass: HomeAssistant, entry_id: str) -> PlantStore:
    """v2 — load the yard's plants (plant-aware irrigation)."""
    from homeassistant.helpers.storage import Store

    helper = Store(hass, STORAGE_VERSION, _storage_key(entry_id, "plants"))
    data = await helper.async_load()
    if not data:
        return PlantStore()
    return PlantStore.from_serializable(data.get("plants", []))


async def _async_load_care_tasks(hass: HomeAssistant, entry_id: str) -> CareTaskStore:
    """v1.35 — load the recurring care-task reminders."""
    from homeassistant.helpers.storage import Store

    helper = Store(hass, STORAGE_VERSION, _storage_key(entry_id, "care_tasks"))
    data = await helper.async_load()
    if not data:
        return CareTaskStore()
    return CareTaskStore.from_serializable(data.get("care_tasks", []))


async def _async_save_care_tasks(hass: HomeAssistant, entry_id: str, store: CareTaskStore) -> None:
    """v1.35 — persist the care-task reminders."""
    from homeassistant.helpers.storage import Store

    helper = Store(hass, STORAGE_VERSION, _storage_key(entry_id, "care_tasks"))
    await helper.async_save({"care_tasks": store.to_serializable()})


async def _async_load_zone_regions(hass: HomeAssistant, entry_id: str) -> ZoneRegionStore:
    """v1.60 — load the drawn zone/loop regions.

    Its OWN store file, deliberately not coord.config: the tick re-serializes
    config every 60 s, and config loads with no validation straight into the
    browser payload. Regions change twice a year and get per-record validation.
    """
    from homeassistant.helpers.storage import Store

    helper = Store(hass, STORAGE_VERSION, _storage_key(entry_id, "zone_regions"))
    data = await helper.async_load()
    if not data:
        return ZoneRegionStore()
    return ZoneRegionStore.from_serializable(data.get("zone_regions", []))


async def _async_save_zone_regions(
    hass: HomeAssistant, entry_id: str, store: ZoneRegionStore
) -> None:
    from homeassistant.helpers.storage import Store

    helper = Store(hass, STORAGE_VERSION, _storage_key(entry_id, "zone_regions"))
    await helper.async_save({"zone_regions": store.to_serializable()})


async def _async_save_plants(hass: HomeAssistant, entry_id: str, store: PlantStore) -> None:
    from homeassistant.helpers.storage import Store

    helper = Store(hass, STORAGE_VERSION, _storage_key(entry_id, "plants"))
    await helper.async_save({"plants": store.to_serializable()})


async def _async_load_lockout(hass: HomeAssistant, entry_id: str) -> LockoutState:
    """Load the rain-lockout snapshot.

    Its own key (``complete_irrigation.rain_lockout.<entry>``), never the
    schedules file. A missing or unreadable file is an empty hold — setup
    must not rewrite schedules to learn that.
    """
    from homeassistant.helpers.storage import Store

    helper = Store(hass, STORAGE_VERSION, _storage_key(entry_id, "rain_lockout"))
    data = await helper.async_load()
    return LockoutState.from_dict(data)


async def _async_save_lockout(hass: HomeAssistant, entry_id: str, state: LockoutState) -> None:
    from homeassistant.helpers.storage import Store

    helper = Store(hass, STORAGE_VERSION, _storage_key(entry_id, "rain_lockout"))
    await helper.async_save(state.to_dict())


class ScheduleCoordinator:
    """Per-entry orchestrator for persisted schedules + per-tick firing."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._store: ScheduleStore = ScheduleStore()
        self._last_tick: datetime | None = None
        self._cancel_tick = None
        # Weather + zone config: {rain_sensor, weather_entity, hot_threshold_f,
        #   boost_percent, zones: {zone_id: {moisture_entities: [...],
        #                                    combine_mode, min, target, max, category}}}
        self._config: dict[str, Any] = {"zones": {}}
        # Lockout state. Deadlines are per durable zone type and live in their
        # own store file. ``lockout_until`` (the property) is the latest of
        # those deadlines so the panel banner / websocket keep one timestamp.
        self._lockouts: dict[str, datetime] = {}
        self._lockout_listeners: list[Callable[[], None]] = []
        self._lockout_published_on = False
        self._cancel_lockout_expiry = None
        self._event_rain_in: float | None = None
        self._event_anchor: datetime | None = None
        self._released_zones: set[str] = set()
        self._forecast_extended_on: dict[str, str] = {}
        self._rain_source_disagree = False
        self._last_tempest_in: float | None = None
        self._last_open_meteo_in: float | None = None
        self._last_combined_in: float | None = None
        self._rain_source = "none"
        self._active_rain = False
        self._meteo_rain_cache: dict[str, Any] | None = None
        self._meteo_rain_cached_at: datetime | None = None
        self._logged_source_fallback: set[str] = set()
        # Notification dispatcher (initialized in async_setup)
        self.notifier: NotificationDispatcher | None = None
        self._cancel_summary = None
        self._cancel_weekly = None
        self._cancel_schedule_review = None  # v1.56 — nightly schedule sweep
        self._cancel_post_install = None
        # Sensor-availability tracking (PRD #57): remembers which bound
        # moisture sensors are currently offline so we only push a
        # notification once per crossing (not on every tick).
        self._offline_sensors: set[str] = set()
        # End-of-establishment tracking (PRD #76): schedule ids we've
        # already prompted about, so the prompt doesn't fire daily.
        self._establishment_prompted: set[str] = set()
        # Run history (v1.14): every completed / skipped / aborted run.
        # Loaded in async_setup; prune() runs at load (age-based retention), and
        # the hard record cap is enforced on every insert (v1.27) so the list
        # stays bounded between restarts.
        self._run_history: RunHistoryStore = RunHistoryStore()
        # v2 — the yard's plants (plant-aware irrigation). Loaded in
        # async_setup; mutated via the add/update/delete_plant services.
        self._plants: PlantStore = PlantStore()
        # v1.19 — auto-soak controller state (in-memory; abandoned on
        # restart, which is safe: the in-flight run's auto-stop handles
        # the valve and a fresh below-min reading restarts the loop).
        # _soak_state[zone_id] = {"phase": "running"|"soaking",
        #                          "cycle": int, "until": datetime}
        # _soak_cooldown[zone_id] = datetime — no re-attempts until then
        self._soak_state: dict[str, dict] = {}
        self._soak_cooldown: dict[str, Any] = {}
        # v1.28 — auto ETo from weather (FAO-56). Refreshed daily at 03:00 and
        # whenever eto_auto is toggled on; effective_eto() falls back to the
        # manual eto_in_week when auto is off, never computed, or stale, so the
        # design math always has a positive number. In-memory by design — a
        # fresh forecast is fetched at startup if auto is on.
        self._auto_eto_in_week: float | None = None
        self._auto_eto_at: datetime | None = None
        # Near-term forecast daily HIGH (F) + when it was cached — fed into the
        # rain-lockout ceiling so it reflects the heat the plants will face
        # DURING the lockout, not just the (often cool) temp when rain falls.
        # Cached alongside the auto-ETo forecast fetch; None when auto-ETo is off.
        self._forecast_high_f: float | None = None
        self._forecast_high_at: datetime | None = None
        self._cancel_eto = None
        # v1.30 — restart fail-over: a one-shot timer that resumes any run
        # interrupted by an HA restart (see _resume_interrupted_runs).
        self._cancel_resume = None
        self._resume_attempts = 0
        # v1.39.1 — when THIS process started (set in async_setup, before the
        # first tick). A persisted session whose started_at predates it was
        # dispatched by a PREVIOUS process: its block timers died with HA, so
        # its recorded block plan describes gaps nothing is enforcing. Gap
        # insertion must not trust such a plan until a successful resume
        # re-records the session with fresh, armed timers — see
        # _gap_trusted_sessions.
        self._uptime_started_at: datetime | None = None
        # v1.39 — occurrence keys (gap_insertion.insertion_key) the resolver
        # has placed in the FUTURE at least once this uptime, with their
        # scheduled starts. This is the stranded-run rescue's eligibility
        # gate: only an occurrence we were actively deferring may be pulled
        # forward to `now` if its placement ever snaps into the past (blocker
        # vanished). Occurrences never planned during uptime — missed during
        # rain lockout or while HA was off — stay ineligible, preserving the
        # deliberate skip-don't-backfire behavior. In-memory BY DESIGN:
        # persisting it would re-enable backfire floods after a restart.
        self._pending_seen: dict[str, datetime] = {}
        # v1.35 — care-task reminders (fertilize/prune/mulch/inspect). Loaded in
        # async_setup; mutated via the care-task services. Due-check runs on the
        # tick, throttled to ~hourly.
        self._care_tasks: CareTaskStore = CareTaskStore()
        self._zone_regions: ZoneRegionStore = ZoneRegionStore()
        self._last_care_check: datetime | None = None
        # v1.35 — active light-survey sessions, keyed by plant id. In-memory by
        # design (a restart abandons the survey; the sensor stake is unharmed and
        # the user simply re-starts). Each: {"sensor": entity_id, "until": datetime,
        # "started": datetime, "minutes": int, "samples": [float, ...]}.
        self._light_survey_sessions: dict[str, dict[str, Any]] = {}
        # v1.55 — area-wide light surveys: one sampling session per AREA whose
        # result fans out to every plant in that area. Keyed by area label; each:
        # {"sensor", "started", "until", "minutes", "samples", "plant_ids": [...]}.
        self._area_survey_sessions: dict[str, dict[str, Any]] = {}

    @property
    def schedule_store(self) -> ScheduleStore:
        return self._store

    @property
    def config(self) -> dict[str, Any]:
        return self._config

    @property
    def independent_zones(self) -> frozenset[str]:
        """Zones on their OWN water supply (e.g. a birdbath fill that does not share the
        drip manifold's pressure). They are exempt from one-zone-at-a-time serialization:
        they fire on schedule, concurrent with any other zone, and never block others.
        Public so the panel / calendar / iCal resolvers apply the same exemption the tick
        does — a partial application would let the zone leak back into serialization."""
        raw = self._config.get("independent_zones") or []
        return frozenset(str(z) for z in raw if z)

    @property
    def run_history(self) -> RunHistoryStore:
        return self._run_history

    @property
    def plants(self) -> PlantStore:
        return self._plants

    @property
    def care_tasks(self) -> CareTaskStore:
        return self._care_tasks

    @property
    def zone_regions(self) -> ZoneRegionStore:
        return self._zone_regions

    def sun_times(self, day):
        """v1.40 — (sunrise, sunset) local-aware datetimes for `day`, or None.
        Passed into next_runs so sun-anchored schedules resolve per-date;
        None (or a raise, which the planner catches) falls back to the
        schedule's fixed start_time."""
        try:
            from homeassistant.helpers.sun import get_astral_event_date
            from homeassistant.util import dt as dt_util

            sunrise = get_astral_event_date(self._hass, "sunrise", day)
            sunset = get_astral_event_date(self._hass, "sunset", day)
            if sunrise is None or sunset is None:
                return None
            return (dt_util.as_local(sunrise), dt_util.as_local(sunset))
        except Exception:
            return None

    def register_lockout_listener(self, callback: Callable[[], None]) -> None:
        self._lockout_listeners.append(callback)

    def _notify_lockout_change(self) -> None:
        for cb in self._lockout_listeners:
            try:
                cb()
            except Exception:
                _LOGGER.exception("Lockout listener errored")

    @property
    def lockout_until(self) -> datetime | None:
        """Latest still-active per-type deadline, or None.

        The panel banner and the websocket payload read this single value.
        A setter is kept so older callers that assigned ``None`` (clear) or a
        datetime (one global hold) still work; the datetime is applied to
        every durable zone type.
        """
        from homeassistant.util import dt as dt_util

        now = dt_util.now()
        future = [
            moment for moment in self._lockouts.values() if moment is not None and moment > now
        ]
        return max(future) if future else None

    @lockout_until.setter
    def lockout_until(self, value: datetime | None) -> None:
        if value is None:
            self._lockouts.clear()
            return
        for zone_type in DURABLE_ZONE_TYPES:
            self._lockouts[zone_type] = value

    def is_locked_out_now(self) -> bool:
        """True while any durable hold is still in the future, or a birdbath
        is paused because rain is falling right now."""
        if self.lockout_until is not None:
            return True
        return bool(self._active_rain and self._any_zone_of_type(TYPE_BIRDBATH))

    async def async_setup(self) -> None:
        from homeassistant.helpers.event import (
            async_track_time_change,
            async_track_time_interval,
        )
        from homeassistant.util import dt as dt_util

        self._store = await _async_load_schedules(self._hass, self._entry_id)
        self._config = await _async_load_config(self._hass, self._entry_id)
        self._config.setdefault("zones", {})
        self._run_history = await _async_load_run_history(self._hass, self._entry_id)
        self._plants = await _async_load_plants(self._hass, self._entry_id)
        self._care_tasks = await _async_load_care_tasks(self._hass, self._entry_id)
        self._zone_regions = await _async_load_zone_regions(self._hass, self._entry_id)
        # Rain lockout has its OWN store. Loading it must not save schedules
        # (or anything else) — a watering hold keeps schedules disabled, and
        # that file has to survive setup byte-for-byte.
        try:
            self._restore_lockout_state(await _async_load_lockout(self._hass, self._entry_id))
        except Exception:
            _LOGGER.exception("Rain lockout load failed; starting with no hold")
        # v1.59 — one-time upgrade: give placements made before v1.59 a durable
        # GROUND anchor, computed against the frame they were placed in. Without
        # it they stay frame-relative, and the next aerial re-fetch at a
        # different centre/zoom would erase them (the bug this replaces).
        # Idempotent, so it is a no-op on every later start.
        try:
            from .yard_map import bbox_from_cfg

            upgraded = self._plants.backfill_anchors(bbox_from_cfg(self._config.get("yard_map")))
            if upgraded:
                await _async_save_plants(self._hass, self._entry_id, self._plants)
                _LOGGER.info(
                    "Anchored %d existing plant marker(s) to their ground position "
                    "— they now survive moving, zooming or rotating the aerial",
                    upgraded,
                )
        except Exception:  # never block startup on a migration
            _LOGGER.exception("plant anchor backfill failed (placements left as-is)")
        # Prune on load — drops anything past the 90-day window so the
        # store file shrinks over time even if the user rarely opens
        # the History tab.
        removed = self._run_history.prune(now=dt_util.now(), retention_days=DEFAULT_RETENTION_DAYS)
        if removed:
            await _async_save_run_history(self._hass, self._entry_id, self._run_history)

        # Notifications
        self.notifier = NotificationDispatcher(self._hass)
        notif_cfg = self._config.get("notifications", {})
        self.notifier.update_config(**notif_cfg)

        # v1.17 — restart-missed lookback. _last_tick is persisted into
        # the config blob on every tick; on startup, if it's within the
        # _MISSED_LOOKBACK_MIN window, resume from there so any runs in
        # the gap (e.g. HA restart spanning a fire-minute) are processed.
        # Long outages (>5 min) fall back to "now" so we don't burst-fire
        # hours of missed runs after, say, a half-day outage.
        saved_last_tick = self._config.get("_last_tick")
        now = dt_util.now()
        # v1.39.1 — mark this uptime's start BEFORE the tick timer arms, so the
        # very first tick can already tell a this-uptime session (armed block
        # timers) from a pre-restart ghost (dead timers, untrusted block plan).
        self._uptime_started_at = now
        if saved_last_tick:
            try:
                saved_dt = datetime.fromisoformat(saved_last_tick)
                # v1.17.1 — convert to local so downstream from_dt.tzinfo
                # matches user-configured start_time semantics. Without
                # this, a UTC-saved last_tick reintroduced the 7-hour
                # firing bug after a restart.
                saved_dt = dt_util.as_local(saved_dt)
                if (now - saved_dt).total_seconds() <= _MISSED_LOOKBACK_MIN * 60:
                    self._last_tick = saved_dt
                    _LOGGER.info(
                        "Resuming from saved last_tick %s (gap %.0fs)",
                        saved_dt.isoformat(),
                        (now - saved_dt).total_seconds(),
                    )
            except (TypeError, ValueError):
                pass
        if self._last_tick is None:
            self._last_tick = now
        self._cancel_tick = async_track_time_interval(
            self._hass, self._tick, timedelta(seconds=TICK_SECONDS)
        )
        # Sensor entities are added after setup. Remember the state they will
        # read on add, and arm a one-shot at the next deadline so the binary
        # sensor is rewritten when the hold expires (it does not poll).
        self._lockout_published_on = self.is_locked_out_now()
        self._arm_lockout_expiry()

        # Morning summary — fires daily at the end of quiet hours. A malformed
        # persisted quiet_hours_end must not break setup -> fall back to 07:00.
        qh_end = _parse_hhmm(self.notifier.quiet_hours_end) or time(7, 0)
        self._cancel_summary = async_track_time_change(
            self._hass, self._fire_morning_summary, hour=qh_end.hour, minute=qh_end.minute, second=0
        )

        # Weekly verification reminder — Sunday 08:00 default
        self._cancel_weekly = async_track_time_change(
            self._hass, self._fire_weekly_reminder, hour=8, minute=0, second=0
        )

        # v1.56 — nightly schedule safety-sweep (21:00): proactively catch any
        # schedule conflict for the coming days and propose a fix (or notify).
        self._cancel_schedule_review = async_track_time_change(
            self._hass, self._fire_nightly_schedule_review, hour=21, minute=0, second=0
        )

        # v1.28 — auto ETo: refresh the FAO-56 figure daily at 03:00 (after
        # midnight, before the morning watering window). If auto is already
        # enabled, fetch one immediately so the design math isn't manual-only
        # until the first 03:00 after a restart.
        self._cancel_eto = async_track_time_change(
            self._hass, self._refresh_auto_eto, hour=3, minute=0, second=0
        )
        if self._config.get("eto_auto"):
            self._hass.async_create_task(self._refresh_auto_eto())

        # PRD #78 — one-shot 7-day post-install reminder. Tracked in
        # coordinator config so it only fires once per integration setup,
        # not on every HA restart.
        # NOTE (v1.38.1): the delayed actions below MUST be @callback — a plain
        # lambda/def is dispatched to an executor THREAD by async_call_later,
        # where hass.async_create_task raises RuntimeError (HA thread-safety
        # guard) and the action silently never runs.
        from homeassistant.core import callback
        from homeassistant.helpers.event import async_call_later

        first_run_at = self._config.get("first_run_at")
        post_install_fired = self._config.get("post_install_reminder_fired", False)
        if first_run_at is None:
            self._config["first_run_at"] = dt_util.now().isoformat()
            await self.async_save_config()
            first_run_at = self._config["first_run_at"]
        if not post_install_fired:
            try:
                first_dt = datetime.fromisoformat(first_run_at)
            except (TypeError, ValueError):
                first_dt = dt_util.now()
            seven_days_later = first_dt + timedelta(days=7)
            delay_seconds = (seven_days_later - dt_util.now()).total_seconds()
            if delay_seconds <= 0:
                # We're past the 7-day mark already — fire on next tick.
                self._hass.async_create_task(self._fire_post_install_reminder())
            else:

                @callback
                def _fire_reminder(_now):
                    self._hass.async_create_task(self._fire_post_install_reminder())

                self._cancel_post_install = async_call_later(
                    self._hass, delay_seconds, _fire_reminder
                )

        # v1.30 — restart fail-over. If HA restarted mid-run, the in-memory
        # auto-stop / chunked-block timers were lost but the run's session was
        # persisted. Resume (or close out) it after a short delay so switch
        # integrations are back online before we re-assert a valve.
        if self._config.get("active_run_sessions"):

            @callback
            def _fire_resume(_now):
                self._hass.async_create_task(self._resume_interrupted_runs())

            self._cancel_resume = async_call_later(self._hass, _RESUME_DELAY_SECONDS, _fire_resume)

        _LOGGER.info(
            "Schedule coordinator started for entry %s — %d schedule(s) loaded",
            self._entry_id,
            len(self._store.all()),
        )

    async def _fire_post_install_reminder(self) -> None:
        """PRD #78 — fires once, 7 days after first setup, prompting the
        user to verify schedules are firing as expected."""
        if not self.notifier:
            return
        if self._config.get("post_install_reminder_fired"):
            return
        zone_count = len(self._config.get("zones", {}))
        sched_count = len(self._store.all())
        await self.notifier.notify(
            f"Your irrigation has been running for a week. "
            f"Currently {sched_count} schedule(s) across {zone_count} zone(s). "
            f"Open the panel to verify recent runs look right and tune any zones.",
            title="Irrigation: 7-day check-in",
            category=CATEGORY_IMPORTANT,
            event_type="post_install_reminder",
        )
        self._config["post_install_reminder_fired"] = True
        await self.async_save_config()

    def async_unload(self) -> None:
        for cancel_attr in (
            "_cancel_tick",
            "_cancel_summary",
            "_cancel_weekly",
            "_cancel_schedule_review",
            "_cancel_post_install",
            "_cancel_eto",
            "_cancel_resume",
            "_cancel_lockout_expiry",
        ):
            cancel = getattr(self, cancel_attr, None)
            if cancel:
                cancel()
                setattr(self, cancel_attr, None)
        _LOGGER.info("Schedule coordinator stopped for entry %s", self._entry_id)

    async def _fire_morning_summary(self, now=None) -> None:
        if self.notifier:
            await self.notifier.flush_morning_summary()
        # Daily low-moisture summary — disabled in notifications config?
        notif_cfg = self._config.get("notifications", {})
        if notif_cfg.get("low_moisture_alerts", True):
            await self._fire_low_moisture_summary()
        # PRD #76: daily check for ended establishment ("New Grass") schedules
        await self._fire_establishment_end_prompts()

    async def _check_sensor_availability(self) -> None:
        """PRD #57 — push a notification when a bound moisture sensor goes
        offline (and a friendly 'back online' note when it recovers).
        Only fires on the transition, not on every tick the sensor is down.
        """
        if not self.notifier:
            return
        zones = self._config.get("zones", {})
        if not zones:
            return
        newly_off, newly_back = detect_sensor_offline_transitions(
            zones,
            lambda eid: self._hass.states.get(eid),
            self._offline_sensors,
        )
        for eid in newly_off:
            state = self._hass.states.get(eid)
            friendly = (state.attributes.get("friendly_name") if state else None) or eid
            await self.notifier.notify(
                (
                    f"Moisture sensor {friendly} is offline — "
                    "low-moisture alerts won't fire for any zone using it "
                    "until it comes back."
                ),
                title="Sensor offline",
                category=CATEGORY_IMPORTANT,
                event_type="sensor_offline",
            )
        for eid in newly_back:
            state = self._hass.states.get(eid)
            friendly = (state.attributes.get("friendly_name") if state else None) or eid
            await self.notifier.notify(
                f"Moisture sensor {friendly} is back online.",
                title="Sensor recovered",
                category=CATEGORY_IMPORTANT,
                event_type="sensor_back_online",
            )
        # Update tracked state
        self._offline_sensors = (self._offline_sensors | newly_off) - newly_back

    async def _fire_establishment_end_prompts(self) -> None:
        """PRD #76 — when a 'New Planting' schedule's end_date passes,
        prompt the user to continue / extend / switch to a normal
        schedule. Only fires once per schedule id.
        """
        if not self.notifier:
            return
        from homeassistant.util import dt as dt_util

        today = dt_util.now().date()
        expired = compute_expired_establishment_schedules(
            self._store.all(), today, self._establishment_prompted
        )
        for s in expired:
            await self.notifier.notify(
                f"Establishment schedule '{s.name}' for {s.zone_entity_id} "
                "has reached its end date. Continue, extend, or switch back "
                "to a normal schedule from the Irrigation panel.",
                title="New planting establishment finished",
                category=CATEGORY_IMPORTANT,
                event_type="establishment_finished",
            )
            self._establishment_prompted.add(s.id)

    async def _fire_low_moisture_summary(self) -> None:
        """Walk zones; notify if any bound moisture sensor is below min%."""
        if not self.notifier:
            return
        offenders = compute_low_moisture_offenders(
            self._config.get("zones", {}),
            lambda eid: self._hass.states.get(eid),
        )
        if not offenders:
            return
        msg = "Moisture below minimum:\n" + "\n".join(offenders)
        await self.notifier.notify(
            msg,
            title="Low moisture — zones need attention",
            category=CATEGORY_IMPORTANT,
            event_type="low_moisture_summary",
        )

    async def _fire_nightly_schedule_review(self, now=None) -> None:
        """v1.56 — nightly safety sweep: detect any schedule conflict and propose a
        fix (LLM configured) or notify for manual adjustment. Propose-only, never
        actuates; wrapped so a review error never disturbs the coordinator."""
        try:
            from .schedule_review import review_schedule

            await review_schedule(self._hass, self, new_schedule_id=None)
        except Exception:
            _LOGGER.exception("nightly schedule review failed (advisory)")

    async def _fire_weekly_reminder(self, now=None) -> None:
        from datetime import date as _date

        from homeassistant.util import dt as dt_util

        n = now or dt_util.now()
        if n.weekday() != 6:  # Sunday only
            return
        if not self.notifier:
            return

        # PRD #81 — snooze check.
        snooze_str = self._config.get("weekly_reminder_snoozed_until")
        if snooze_str:
            try:
                snoozed_until = _date.fromisoformat(snooze_str)
                if n.date() < snoozed_until:
                    _LOGGER.info("Weekly reminder snoozed until %s — skipping", snoozed_until)
                    return
            except (TypeError, ValueError):
                pass  # bad value → ignore, fire as normal

        # PRD #80 — per-zone summary instead of a single aggregate line.
        # Pulls zones from the integration entry (not coord.config) so
        # we list zones even before they've had a moisture binding.
        zone_ids: list[str] = []
        for data in self._hass.data.get(DOMAIN, {}).values():
            if isinstance(data, dict):
                zone_ids.extend(data.get("zones") or [])
        # De-duplicate but preserve order
        seen: set[str] = set()
        zone_ids = [z for z in zone_ids if not (z in seen or seen.add(z))]

        per_zone = build_weekly_zone_summary(
            zone_ids,
            self._store.enabled_schedules(),
            self._config.get("zones", {}),
            lambda eid: self._hass.states.get(eid),
        )
        header = (
            f"Weekly check ({len(self._store.all())} schedule(s) across {len(zone_ids)} zone(s)):"
        )
        body = "\n".join(per_zone) if per_zone else "No zones configured yet."
        msg = f"{header}\n{body}"

        await self.notifier.notify(
            msg,
            title="Weekly irrigation check",
            category=CATEGORY_IMPORTANT,
            event_type="weekly_reminder",
        )

    async def async_save(self) -> None:
        await _async_save_schedules(self._hass, self._entry_id, self._store)

    async def async_save_config(self) -> None:
        await _async_save_config(self._hass, self._entry_id, self._config)

    async def async_save_run_history(self) -> None:
        await _async_save_run_history(self._hass, self._entry_id, self._run_history)

    async def async_save_plants(self) -> None:
        await _async_save_plants(self._hass, self._entry_id, self._plants)

    async def async_save_care_tasks(self) -> None:
        await _async_save_care_tasks(self._hass, self._entry_id, self._care_tasks)

    async def async_save_zone_regions(self) -> None:
        await _async_save_zone_regions(self._hass, self._entry_id, self._zone_regions)

    async def async_save_lockout(self) -> None:
        """Persist per-type lockout deadlines. Never writes the schedules file."""
        try:
            await _async_save_lockout(self._hass, self._entry_id, self._lockout_state())
        except Exception:
            _LOGGER.exception("Rain lockout save failed")

    def _spawn(self, coro) -> None:
        """Schedule ``coro`` on the HA loop.

        A test double's ``async_create_task`` accepts the coroutine and drops
        it; close it in that case so the suite's warning-as-error filter stays
        quiet. A real Home Assistant task is left running.
        """
        try:
            task = self._hass.async_create_task(coro)
        except Exception:
            coro.close()
            return
        if not asyncio.isfuture(task):
            coro.close()

    # ── Light surveys (v1.35) ──────────────────────────────────────

    def start_light_survey(self, plant_id: str, sensor_entity_id: str, minutes: int) -> None:
        """Register a survey session; the tick samples it. Caller validated inputs."""
        from homeassistant.util import dt as dt_util

        now = dt_util.now()
        self._light_survey_sessions[plant_id] = {
            "sensor": sensor_entity_id,
            "started": now,
            "until": now + timedelta(minutes=minutes),
            "minutes": minutes,
            "samples": [],
        }

    def cancel_light_survey(self, plant_id: str) -> bool:
        return self._light_survey_sessions.pop(plant_id, None) is not None

    def light_survey_status(self) -> dict[str, dict[str, Any]]:
        """Active sessions for the WS layer/panel: plant_id -> progress info."""
        out: dict[str, dict[str, Any]] = {}
        for pid, s in self._light_survey_sessions.items():
            out[pid] = {
                "sensor": s["sensor"],
                "started": s["started"].isoformat(),
                "until": s["until"].isoformat(),
                "minutes": s["minutes"],
                "samples": len(s["samples"]),
            }
        return out

    def _sample_light_sensor(self, entity_id: str) -> float | None:
        state = self._hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    async def _tick_light_surveys(self, now: datetime) -> None:
        """Sample each active survey once per tick; finish + store when due."""
        if not self._light_survey_sessions:
            return
        finished: list[str] = []
        for pid, sess in self._light_survey_sessions.items():
            val = self._sample_light_sensor(sess["sensor"])
            if val is not None:
                sess["samples"].append(val)
            if now >= sess["until"]:
                finished.append(pid)
        for pid in finished:
            # Race-tolerant pop: the await below yields, so a cancel_light_survey /
            # delete_plant landing mid-batch may have already removed a later pid —
            # skip it rather than KeyError-aborting the rest of the batch.
            sess = self._light_survey_sessions.pop(pid, None)
            if sess is None:
                continue
            await self._finish_light_survey(pid, sess, now)

    async def _finish_light_survey(
        self, plant_id: str, sess: dict[str, Any], now: datetime
    ) -> None:
        from dataclasses import replace as _dc_replace

        plant = self._plants.get(plant_id)
        summary = summarize_samples(sess["samples"])
        if plant is None or summary is None:
            # Plant deleted mid-survey, or the sensor never produced a usable
            # reading — report the failure instead of storing a lie.
            name = plant.name if plant is not None else plant_id
            _LOGGER.warning(
                "Light survey for %s produced no usable samples (sensor %s)", name, sess["sensor"]
            )
            if self.notifier is not None and plant is not None:
                await self.notifier.notify(
                    f"Light survey for {plant.name} collected no usable readings from "
                    f"{sess['sensor']} — check the sensor and try again.",
                    title="Light survey failed",
                    category=CATEGORY_INFORMATIONAL,
                    event_type="light_survey_failed",
                )
            return
        verdict = light_verdict(summary["lux_avg"], plant.lux_low, plant.lux_high)
        entry = make_survey_entry(
            ts=int(now.timestamp()),
            minutes=sess["minutes"],
            sensor_entity_id=sess["sensor"],
            summary=summary,
            verdict=verdict,
        )
        surveys = (entry, *plant.light_surveys)[:_MAX_SURVEYS_KEPT]
        self._plants.upsert(_dc_replace(plant, light_surveys=surveys))
        await self.async_save_plants()
        if self.notifier is not None:
            await self.notifier.notify(
                f"{verdict_text(verdict, plant.name)} "
                f"(avg {summary['lux_avg']:.0f} lux over {summary['samples']} readings"
                + (
                    f"; optimal {plant.lux_low}-{plant.lux_high} lux)"
                    if plant.lux_low is not None
                    else ")"
                ),
                title="Light survey complete",
                category=CATEGORY_INFORMATIONAL,
                event_type="light_survey_complete",
            )

    # ── Area-wide light surveys (v1.55) ────────────────────────────
    def start_area_light_survey(
        self, area: str, plant_ids: list[str], sensor_entity_id: str, minutes: int
    ) -> None:
        """Register ONE survey session for a light area; the tick samples it and,
        on completion, applies the reading to every listed plant. Caller validated."""
        from homeassistant.util import dt as dt_util

        now = dt_util.now()
        self._area_survey_sessions[area] = {
            "sensor": sensor_entity_id,
            "started": now,
            "until": now + timedelta(minutes=minutes),
            "minutes": minutes,
            "samples": [],
            "plant_ids": list(plant_ids),
        }

    def cancel_area_light_survey(self, area: str) -> bool:
        return self._area_survey_sessions.pop(area, None) is not None

    def area_survey_status(self) -> dict[str, dict[str, Any]]:
        """Active area sessions for the WS layer/panel: area -> progress info."""
        out: dict[str, dict[str, Any]] = {}
        for area, s in self._area_survey_sessions.items():
            out[area] = {
                "sensor": s["sensor"],
                "started": s["started"].isoformat(),
                "until": s["until"].isoformat(),
                "minutes": s["minutes"],
                "samples": len(s["samples"]),
                "plants": len(s["plant_ids"]),
            }
        return out

    async def _tick_area_surveys(self, now: datetime) -> None:
        """Sample each active area survey once per tick; finish + fan out when due."""
        if not self._area_survey_sessions:
            return
        finished: list[str] = []
        for area, sess in self._area_survey_sessions.items():
            val = self._sample_light_sensor(sess["sensor"])
            if val is not None:
                sess["samples"].append(val)
            if now >= sess["until"]:
                finished.append(area)
        for area in finished:
            sess = self._area_survey_sessions.pop(area, None)
            if sess is None:  # race: cancelled mid-batch
                continue
            await self._finish_area_light_survey(area, sess, now)

    async def _finish_area_light_survey(
        self, area: str, sess: dict[str, Any], now: datetime
    ) -> None:
        """Store the one reading on every plant still in the area, each with its own
        verdict vs its own optimal range. One summary notification for the area."""
        from dataclasses import replace as _dc_replace

        summary = summarize_samples(sess["samples"])
        if summary is None:
            _LOGGER.warning(
                "Area light survey %r produced no usable samples (sensor %s)",
                area,
                sess["sensor"],
            )
            if self.notifier is not None:
                await self.notifier.notify(
                    f"Light survey for area '{area}' collected no usable readings from "
                    f"{sess['sensor']} — check the sensor and try again.",
                    title="Area light survey failed",
                    category=CATEGORY_INFORMATIONAL,
                    event_type="area_light_survey_failed",
                )
            return
        ts = int(now.timestamp())
        applied = 0
        for pid in sess["plant_ids"]:
            plant = self._plants.get(pid)
            if plant is None:  # deleted mid-survey — skip, don't store a lie
                continue
            verdict = light_verdict(summary["lux_avg"], plant.lux_low, plant.lux_high)
            entry = make_survey_entry(
                ts=ts,
                minutes=sess["minutes"],
                sensor_entity_id=sess["sensor"],
                summary=summary,
                verdict=verdict,
            )
            surveys = (entry, *plant.light_surveys)[:_MAX_SURVEYS_KEPT]
            self._plants.upsert(_dc_replace(plant, light_surveys=surveys))
            applied += 1
        if applied:
            await self.async_save_plants()
        if self.notifier is not None:
            await self.notifier.notify(
                f"Light survey for area '{area}' complete — avg "
                f"{summary['lux_avg']:.0f} lux over {summary['samples']} readings, "
                f"applied to {applied} plant(s).",
                title="Area light survey complete",
                category=CATEGORY_INFORMATIONAL,
                event_type="area_light_survey_complete",
            )

    # ── Watering diagnosis (v1.35) ─────────────────────────────────

    def diagnose_zone_watering(self, zone_entity_id: str):
        """Assemble live inputs for the pure diagnosis rail. Advisory only."""
        from homeassistant.util import dt as dt_util

        from .watering_diagnosis import diagnose_zone

        zone_cfg = (self._config.get("zones") or {}).get(zone_entity_id) or {}
        current = None
        if zone_cfg.get("moisture_entities"):
            current, analysis_ids, _readings, _excl = self._zone_moisture_reading(zone_cfg)
            if not analysis_ids:
                current = None

        def _pct(key: str, default: float | None) -> float | None:
            v = zone_cfg.get(key, default)
            try:
                f = float(v)
            except (TypeError, ValueError):
                return None
            return f if math.isfinite(f) else None

        # Vision-health concerns from this zone's plants (bounded by the v1.33 rail).
        concerns: list[str] = []
        # v1.36 — the plants' latest light-survey verdicts (fresh within 120 days)
        # as corroborating stress signals for the rail.
        light_verdicts: list[dict] = []
        now_ts = dt_util.now().timestamp()
        fresh_cutoff = now_ts - 120 * 86400
        fresh_ceiling = now_ts + 86400  # a day of clock-skew slack; future-dated = stale
        for p in self._plants.by_zone(zone_entity_id):
            if isinstance(p.health, dict):
                concerns.extend(
                    str(c) for c in (p.health.get("concerns") or []) if isinstance(c, str)
                )
            if p.light_surveys:
                latest = p.light_surveys[0]  # newest-first, rail-cleaned on load
                if fresh_cutoff <= latest.get("ts", 0) <= fresh_ceiling:
                    light_verdicts.append({"plant": p.name, "verdict": latest.get("verdict")})

        # Thresholds default to the zone-config values only when sensors exist;
        # a sensorless zone keeps None thresholds -> STATUS_UNKNOWN from the rail.
        have_sensors = bool(zone_cfg.get("moisture_entities"))
        return diagnose_zone(
            zone_entity_id=zone_entity_id,
            now=dt_util.now(),
            current_pct=current,
            min_pct=_pct("min_pct", DEFAULT_ZONE_MIN_PCT) if have_sensors else None,
            target_pct=_pct("target_pct", DEFAULT_ZONE_TARGET_PCT) if have_sensors else None,
            max_pct=_pct("max_pct", DEFAULT_ZONE_MAX_PCT) if have_sensors else None,
            history=self._run_history.all(),
            plant_concerns=concerns,
            light_verdicts=light_verdicts,
        )

    # ── Care-task reminders (v1.35) ────────────────────────────────

    async def _tick_care_reminders(self, now: datetime) -> None:
        """Hourly: notify care tasks that are due (weekly re-nag, quiet-hours
        handled by the notifier's own delivery rules)."""
        if self._last_care_check is not None and now - self._last_care_check < timedelta(hours=1):
            return
        self._last_care_check = now
        now_ts = int(now.timestamp())
        due = self._care_tasks.needing_reminder(now_ts)
        if not due:
            return
        changed = False
        for task in due:
            subject = None
            if task.plant_id:
                plant = self._plants.get(task.plant_id)
                if plant is None:
                    continue  # dangling task; leave for the user to clean up
                subject = plant.name
            else:
                zone_state = self._hass.states.get(task.zone_entity_id)
                subject = (
                    zone_state.attributes.get("friendly_name") if zone_state else None
                ) or task.zone_entity_id
            if self.notifier is not None:
                await self.notifier.notify(
                    f"{task.display_name()} — {subject} is due (every {task.interval_days} "
                    "days). Mark it done in the panel's Yard tab when finished.",
                    title="Plant care due",
                    category=CATEGORY_INFORMATIONAL,
                    event_type="care_task_due",
                )
            self._care_tasks.upsert(task.notified(now_ts))
            changed = True
        if changed:
            await self.async_save_care_tasks()

    def clear_lockout(self) -> None:
        """End every per-type hold. The schedules file is not touched."""
        if not self._lockouts and not self._released_zones and not self._active_rain:
            return
        self._lockouts.clear()
        self._released_zones.clear()
        self._event_rain_in = None
        self._event_anchor = None
        self._forecast_extended_on.clear()
        self._active_rain = False
        self._publish_lockout()
        self._spawn(self.async_save_lockout())

    # ── Tick logic ─────────────────────────────────────────────────

    async def _tick(self, now: datetime) -> None:
        # v1.17.1 — `now` from async_track_time_interval is UTC. Convert
        # to local immediately so every downstream calc uses local-aware
        # datetimes. Critical for the planner: it uses from_dt.tzinfo to
        # localize each schedule's start_time, and a UTC tzinfo on a
        # user-entered "14:30" produced "14:30 UTC" = 07:30 local,
        # firing every schedule ~UTC-offset hours early.
        from homeassistant.util import dt as dt_util

        now = dt_util.as_local(now)
        if self._last_tick is None:
            self._last_tick = now
            return

        # Evaluate rain lockout on every tick.
        await self._evaluate_lockout(now)

        # PRD #57: per-tick sensor-offline detection. Pushes one
        # notification per crossing; clears when sensor recovers.
        await self._check_sensor_availability()

        last = self._last_tick
        self._last_tick = now
        # v1.17 — persist last_tick so a restart within the lookback
        # window resumes from here instead of dropping the gap.
        self._config["_last_tick"] = now.isoformat()
        # Fire-and-forget the save (no await); Store serializes writes
        # internally and we'd rather not block the tick on disk I/O.
        self._hass.async_create_task(self.async_save_config())

        # v1.19 — drive the auto-soak state machines (guards rain
        # lockout internally; runs even when the schedule path below
        # early-returns during lockout).
        await self._tick_auto_soak(now)

        # v1.35 — sample active light surveys + hourly care-reminder check.
        # Both are advisory-only and must never break the watering tick.
        try:
            await self._tick_light_surveys(now)
            await self._tick_area_surveys(now)
            await self._tick_care_reminders(now)
        except Exception:
            _LOGGER.exception("Care/light tick failed (advisory features; watering unaffected)")

        # Per-zone-type rain lockout. A schedule with ignore_rain_lockout
        # still fires. Locked schedules are taken out of planning so they
        # don't occupy the manifold and aren't backfired when the hold ends
        # (last_tick advances either way). An occurrence whose slot falls in
        # (last, now] is written to run history as a skip.
        zone_buffer = self._config.get("zone_buffer_seconds")
        enabled_scheds = self._store.enabled_schedules()
        locked_scheds = [
            sched
            for sched in enabled_scheds
            if not sched.ignore_rain_lockout and self.schedule_is_locked(sched, now)
        ]
        if locked_scheds:
            await self._record_lockout_skips(locked_scheds, last, now, zone_buffer)
            locked_ids = {sched.id for sched in locked_scheds}
            enabled_scheds = [sched for sched in enabled_scheds if sched.id not in locked_ids]
            if not enabled_scheds:
                return
            _LOGGER.info(
                "Rain lockout active; %d schedule(s) still firing",
                len(enabled_scheds),
            )
        # v1.20 — size the window to the longest run so the resolver can see
        # a multi-hour run that started before the window and is still going
        # (the old fixed 2h blinded it to 4-5h Trees/Citrus cycles). Floor at
        # the cascade cap so within-cap deferrals are always visible too.
        horizon = timedelta(
            minutes=max(
                DEFAULT_CASCADE_CAP_MINUTES,
                longest_run_span_minutes(enabled_scheds, zone_buffer) + DEFAULT_BUFFER_MINUTES,
            )
        )
        upcoming = next_runs(
            enabled_scheds,
            from_dt=last - horizon,
            until_dt=now + horizon,
            zone_buffer_seconds=int(zone_buffer) if zone_buffer is not None else None,
            sun_times=self.sun_times,
        )
        # Honor a user-selected global conflict policy (Settings tab).
        # Unknown / missing → keep safe default of POLICY_DEFER_NEW.
        configured = self._config.get("conflict_policy", POLICY_DEFER_NEW)
        policy = configured if configured in _VALID_CONFLICT_POLICIES else POLICY_DEFER_NEW
        # v1.32 — one-zone-at-a-time across LIVE runs. Feed the resolver the runs
        # already in progress (manual, scheduled, or resumed after a restart) as
        # fixed COMMITTED blockers, so a due scheduled run is DEFERRED past them
        # instead of firing a second zone into a controller that runs one zone at a
        # time. The live registry is active_run_sessions (persisted by the run
        # services). A manual run is invisible to next_runs (it's not a schedule),
        # so this is the only thing that stops a schedule colliding with it.
        # Self-heal orphaned sessions first: a chunked run interrupted before its final
        # block (e.g. an HA restart mid-sequence) never clears its record, and the stale
        # session then phantom-reserves the one-zone controller for its whole original
        # window — silently blocking other zones. Prune the finished ones every tick.
        self._prune_stale_sessions(now)
        independent = self.independent_zones
        committed = committed_runs_from_sessions(
            self._config.get("active_run_sessions") or {}, now, independent_zones=independent
        )
        if committed:
            # Drop the already-fired scheduled occurrence a committed session stands
            # in for (its start is in the past) so the resolver models ONE busy
            # interval, not the committed run plus its adjustable twin. A FUTURE
            # occurrence of the same schedule stays — it must route around the run.
            fired = {(c.schedule_id, c.zone_entity_id) for c in committed if c.schedule_id}
            upcoming = [
                r
                for r in upcoming
                if (r.schedule_id, r.zone_entity_id) not in fired or r.start_at > now
            ]
        # v1.39 — gap-aware short-run insertion. A short scheduled run that fits
        # ENTIRELY inside a live chunked run's inter-block gap (with a safety
        # margin both sides — see gap_insertion.py) is pinned into that gap
        # instead of deferring hours behind the whole gap-inclusive span. The
        # pinned run is FIXED for the resolver (never moved; the long run's
        # block cadence is timer-driven and untouched) and fires through the
        # normal due path, so moisture/wind/hot-weather gates still evaluate at
        # fire time. Runs that fit no gap resolve exactly as before.
        #
        # v1.39 — fired-occurrence ledger: EVERY scheduled firing is recorded
        # (_record_run_fired) and its occurrence DROPPED from planning here.
        # A fired occurrence must never be re-planned: a gap-inserted one would
        # be re-inserted into the long run's NEXT gap (its scheduled start is
        # still blocked) and watered twice, and a finished chunked session's
        # occurrence used to re-enter planning as a past 'ghost' adjustable run
        # whose sweep cursor carried its WATER duration — gap-total SHORTER
        # than the real wall span — snapping runs chain-deferred behind it into
        # the past, out of the (last, now] due window, silently LOST (the
        # v1.38.x chain-deferral loss). With fired occurrences provably out of
        # planning, the resolver's stranded-run rescue below is safe: it can
        # only ever pull forward a run that truly never fired.
        fired_ledger = self._config.get("fired_occurrences") or {}
        if fired_ledger:
            upcoming = [r for r in upcoming if insertion_key(r) not in fired_ledger]
        insertions = plan_gap_insertions(
            upcoming,
            # v1.39.1 — only trust block plans whose timers are armed in THIS
            # uptime; a pre-restart ghost plan (or a resume-gated session)
            # stays a busy span but exposes no gaps (see _gap_trusted_sessions).
            self._gap_trusted_sessions(self._config.get("active_run_sessions") or {}),
            now,
            zone_buffer_seconds=int(zone_buffer) if zone_buffer is not None else None,
            tick_seconds=TICK_SECONDS,
            independent_zones=independent,
            fired_keys=frozenset(fired_ledger),
            # v1.39.1 — a run over the controller cap would be CHUNKED at
            # dispatch (blocks + inter-block gaps) and burst out of its host
            # gap; the planner must never pin one (see gap_insertion.py).
            controller_cap_minutes=int(
                self._config.get("controller_max_run_minutes", DEFAULT_CONTROLLER_MAX_RUN_MIN)
            ),
        )
        if insertions:
            upcoming = [
                (
                    replace(r, start_at=pin, reason=REASON_GAP_INSERTED)
                    if (pin := insertions.get(insertion_key(r))) is not None
                    else r
                )
                for r in upcoming
            ]
            _LOGGER.debug("Gap insertion plan this tick: %s", insertions)
        # v1.20 — NEVER-DROP. A run that can't be placed within the cascade cap is
        # deferred *late* and still fires, instead of being skipped. Compression
        # (shrinking adjustable runs) stays OFF (compress_floor_pct=100): we only
        # DEFER around committed runs, never compress one that has already started.
        resolved = resolve_conflicts(
            committed + upcoming,
            policy,
            compress_floor_pct=100,  # 100 = no compression (defer-late only)
            independent_zones=independent,
            # v1.39 — never-drop backstop: an unfired occurrence whose
            # placement snapped into the past (its blocker vanished, e.g. a
            # pruned chunked session) is re-placed at `now` so this tick's
            # due window catches it — but ONLY if we were actively deferring
            # it this uptime (_pending_seen), so runs deliberately skipped
            # during rain lockout or HA downtime are never backfired.
            rescue_stranded_before=last,
            rescue_stranded_at=now,
            rescue_eligible=lambda r: insertion_key(r) in self._pending_seen,
        )
        # Remember every occurrence currently planned in the future that was
        # genuinely DISPLACED past its scheduled start — deferred behind a
        # blocker, or pinned into a gap — the rescue-eligibility gate above.
        # v1.39.1: recording EVERY future-planned occurrence made slots that
        # later fell inside a rain lockout rescue-eligible (each pre-lockout
        # tick had already enumerated them), so the first post-lockout tick
        # backfired them all at once — the exact v1.17.3 flood the design
        # forbids. A never-displaced run needs no rescue eligibility: if its
        # slot arrives while HA is up and unlocked it fires via the
        # (last, now] due window; if the slot passes during lockout/downtime
        # it stays ineligible — skip, don't backfire. In-memory; pruned by
        # scheduled age once it grows (entries clear via _record_run_fired).
        for r in resolved:
            if (
                r.reason != REASON_COMMITTED
                and r.start_at > now
                and (
                    r.reason == REASON_GAP_INSERTED
                    or (r.scheduled_start_at is not None and r.start_at > r.scheduled_start_at)
                )
            ):
                self._pending_seen[insertion_key(r)] = r.scheduled_start_at
        if len(self._pending_seen) > 512:
            cutoff = now - timedelta(hours=48)
            self._pending_seen = {k: v for k, v in self._pending_seen.items() if v >= cutoff}
        due = due_runs_since(resolved, last, now)
        # Committed runs are blockers ONLY — never (re)fire one even if its actual
        # start_at falls inside this tick's (last, now] window (e.g. a manual run
        # that started seconds ago would otherwise be double-fired).
        due = [r for r in due if r.reason != REASON_COMMITTED]

        for run in due:
            # v1.20 — the resolver never drops a run anymore (compress →
            # defer-late instead). This branch now only catches a defensive
            # "skipped:" reason should one ever appear; the normal path fires
            # every due run. A run squeezed past the cap (deferred-late) still
            # fires, but we alert so the user knows the schedule is overpacked.
            if run.reason == REASON_DEFERRED_LATE:
                await self._notify_deferred_late(run)
            if run.reason.startswith("skipped:"):
                _LOGGER.info(
                    "Skipping run for %s (%s): %s",
                    run.zone_entity_id,
                    run.schedule_name,
                    run.reason,
                )
                # v1.16.2 — also write a SKIPPED record to History so the
                # user sees these in the panel (gate-skips from _fire_run,
                # plus any defensive resolver skip).
                zone_state = self._hass.states.get(run.zone_entity_id)
                zone_name = (
                    zone_state.attributes.get("friendly_name") if zone_state else None
                ) or run.zone_entity_id
                rec = self._run_history.record_skipped(
                    zone_entity_id=run.zone_entity_id,
                    zone_name=zone_name,
                    requested_minutes=run.duration_minutes,
                    schedule_id=run.schedule_id,
                    schedule_name=run.schedule_name,
                    fired_at=now,
                    reason=run.reason,
                    triggers={"conflict_resolver": {"verdict": run.reason}},
                )
                await self.async_save_run_history()
                # v1.17 — actionable "Run now" notification
                await self._notify_missed_run(rec, run)
                continue
            # Ledger BEFORE firing: whatever the gates decide (fire or skip),
            # this occurrence is DONE — dropped from every later tick's
            # planning (see the fired-occurrence ledger comment above).
            self._record_run_fired(run, now)
            await self._fire_run(run)

    def _record_run_fired(self, run, now: datetime) -> None:
        """v1.39 — the fired-occurrence ledger: remember that this scheduled
        occurrence has been consumed (fired, or gate-skipped at fire time),
        keyed by gap_insertion.insertion_key. The tick loop drops ledgered
        occurrences from planning entirely, replacing the old implicit
        protection (a fired run stranding behind the due window once its
        blockers cleared) — which broke down for chunked runs, whose 'ghost'
        occurrence carried water time instead of wall time and mis-placed the
        runs chained behind it (the v1.38.x chain-deferral loss). It also
        keeps a gap-inserted occurrence from being re-inserted into the long
        run's next gap and watered twice, and makes the resolver's stranded-
        run rescue safe (it can never pull forward an already-fired run).
        Entries prune after 24h — far beyond the longest possible blocking
        session (MAX_SCHEDULE_DURATION_MIN = 8h) — and the ledger persists in
        config so a restart can't forget a firing."""
        ledger = self._config.setdefault("fired_occurrences", {})
        ledger[insertion_key(run)] = now.isoformat()
        self._pending_seen.pop(insertion_key(run), None)
        cutoff = now - timedelta(hours=24)
        for key, fired_at in list(ledger.items()):
            try:
                if datetime.fromisoformat(fired_at) < cutoff:
                    del ledger[key]
            except (TypeError, ValueError):
                del ledger[key]  # malformed — drop rather than keep forever
        if run.reason == REASON_GAP_INSERTED:
            _LOGGER.info(
                "Firing %s (%s) inside a chunked run's inter-block gap at %s",
                run.zone_entity_id,
                run.schedule_name,
                run.start_at,
            )
        self._hass.async_create_task(self.async_save_config())

    async def _notify_missed_run(self, record, run) -> None:
        """v1.17 — actionable notification for a skipped/dropped run.

        Sends one notification per skip with a single "Run now (X min)"
        action button that, when tapped, triggers run_zone with the
        original planned duration. Honors the `notify_on_missed`
        setting (default true) and the notifier's enabled/quiet-hours
        rules just like any other notification.

        The action payload is packed directly into the button's `data`
        field (mobile_app spec) so the event listener doesn't need any
        side-table — entirely stateless.
        """
        if self.notifier is None:
            return
        notif_cfg = self._config.get("notifications", {})
        if not notif_cfg.get("notify_on_missed", True):
            return
        reason_short = run.reason.replace("skipped:", "").strip() or "system"
        message = (
            f"{record.schedule_name} ({record.zone_name}) was skipped — "
            f"{reason_short}. Tap to run now ({record.requested_minutes} min)."
        )
        await self.notifier.notify(
            message,
            title="Irrigation: Run missed",
            category=CATEGORY_IMPORTANT,
            event_type="run_missed",
            actions=[
                {
                    "action": RUN_MISSED_ACTION,
                    "title": f"Run now ({record.requested_minutes} min)",
                    "data": {
                        "zone_entity_id": record.zone_entity_id,
                        "minutes": record.requested_minutes,
                        "missed_record_id": record.id,
                        "schedule_id": record.schedule_id,
                        "schedule_name": record.schedule_name,
                    },
                },
                {"action": f"{RUN_MISSED_ACTION}_DISMISS", "title": "Dismiss"},
            ],
        )

    async def _notify_deferred_late(self, run) -> None:
        """v1.20 — alert when a run had to be deferred past the cascade cap.

        The run still fires (never dropped), but landing this far past its
        scheduled time means the schedules are oversubscribed — too many
        overlapping runs for one-zone-at-a-time to fit. Surfacing it lets
        the user space their start times before a hot afternoon backs up.
        Honors `notify_on_missed` (the same late/missed family opt-out).
        """
        if self.notifier is None:
            return
        notif_cfg = self._config.get("notifications", {})
        if not notif_cfg.get("notify_on_missed", True):
            return
        zone_state = self._hass.states.get(run.zone_entity_id)
        zone_name = (
            zone_state.attributes.get("friendly_name") if zone_state else None
        ) or run.zone_entity_id
        await self.notifier.notify(
            f"{run.schedule_name} ({zone_name}) is running late — the schedule is "
            f"oversubscribed (too many overlapping runs to fit one-zone-at-a-time). "
            f"It WILL still run; consider spacing your schedule start times.",
            title="Irrigation: running late",
            category=CATEGORY_IMPORTANT,
            event_type="run_deferred_late",
        )

    async def _fire_run(self, run) -> None:
        """Evaluate moisture + wind + hot-weather modifiers for `run`, then fire it.

        Builds a `triggers` snapshot dict along the way so the run history
        can show what gates were evaluated and how they decided. If any
        gate decides to skip, records a SKIPPED entry directly to history
        (the service path is not taken). Otherwise stashes the metadata
        in entry_data so the service handler can include it on the start
        record.
        """
        from homeassistant.util import dt as dt_util

        zone_id = run.zone_entity_id
        zone_cfg = self._config["zones"].get(zone_id, {})
        adjusted_minutes = run.duration_minutes
        skip_reasons: list[str] = []
        boost_reasons: list[str] = []
        triggers: dict[str, Any] = {}

        # v1.17.3 — look up the schedule for per-schedule gate opt-outs
        # (ignore_wind / ignore_hot_weather). Moisture gate still runs
        # because it's per-zone — there's no analogous "ignore" since
        # the user just wouldn't bind a moisture sensor to a bird-bath
        # zone.
        sched = self._store.get(run.schedule_id)
        ignore_wind = bool(sched and sched.ignore_wind)
        ignore_hot = bool(sched and sched.ignore_hot_weather)

        adjusted_minutes, m_skip = self._evaluate_moisture_gate(
            zone_cfg, zone_id, adjusted_minutes, triggers
        )
        if m_skip:
            skip_reasons.append(m_skip)
        if not skip_reasons and not ignore_wind:
            w_skip = self._evaluate_wind_gate(triggers)
            if w_skip:
                skip_reasons.append(w_skip)
        elif ignore_wind:
            # Note in triggers so History shows the gate was deliberately skipped
            triggers["wind"] = {"ignored_by_schedule": True}
        if not skip_reasons and not ignore_hot:
            adjusted_minutes, boost_note = self._evaluate_hot_weather_gate(
                adjusted_minutes, triggers
            )
            if boost_note:
                boost_reasons.append(boost_note)
        elif ignore_hot:
            triggers["hot_weather"] = {"ignored_by_schedule": True}

        # v1.39 — a gap-inserted run was sized to fit its inter-block gap with
        # margin; a hot-weather boost could stretch it past the tail margin
        # INTO the long run's next block (two valves open — the inviolable
        # one-zone-at-a-time break). Clamp back to the planned duration:
        # extension is dangerous, shortening (moisture gate) is always fine.
        if run.reason == REASON_GAP_INSERTED:
            gap_note: dict[str, Any] = {"fired_in_gap": True}
            if adjusted_minutes > run.duration_minutes:
                gap_note["boost_clamped_to_fit_gap"] = True
                adjusted_minutes = run.duration_minutes
            # v1.39.1 belt — run_zone CHUNKS anything over the controller cap
            # into blocks separated by block_gap_seconds; the chunked wall time
            # bursts out of the host gap into the long run's next block (two
            # valves on the shared manifold). plan_gap_insertions never pins a
            # would-chunk run, so this only trips if the cap was lowered between
            # planning and firing — skip instead of dispatching. The skip path
            # records history + sends the actionable "Run now" notification,
            # and the ledger already consumed the occurrence, so it is never
            # re-inserted into a later gap.
            cap = int(
                self._config.get("controller_max_run_minutes", DEFAULT_CONTROLLER_MAX_RUN_MIN)
            )
            if adjusted_minutes > cap:
                gap_note["exceeds_controller_cap"] = cap
                skip_reasons.append(
                    f"gap-inserted run of {adjusted_minutes} min exceeds the {cap}-min "
                    "controller cap (chunked delivery would burst its host gap)"
                )
            triggers["gap_insertion"] = gap_note

        # Resolve a friendly zone name (state attribute → entity_id fallback).
        zone_state = self._hass.states.get(zone_id)
        zone_name = (zone_state.attributes.get("friendly_name") if zone_state else None) or zone_id

        if skip_reasons:
            _LOGGER.info(
                "Skipped scheduled run for %s: %s",
                zone_id,
                "; ".join(skip_reasons),
            )
            rec = self._run_history.record_skipped(
                zone_entity_id=zone_id,
                zone_name=zone_name,
                requested_minutes=run.duration_minutes,
                schedule_id=run.schedule_id,
                schedule_name=run.schedule_name,
                fired_at=dt_util.now(),
                reason="; ".join(skip_reasons),
                triggers=triggers,
            )
            await self.async_save_run_history()
            # v1.17 — also send the actionable "Run now?" notification
            # for gate skips (moisture / wind / hot-weather decisions).
            # The notifier honors `notify_on_missed` so users can opt
            # out without losing other notifications.
            await self._notify_missed_run(rec, run)
            return

        for r in boost_reasons:
            _LOGGER.info("Adjusted run for %s: %s", zone_id, r)

        # Stash metadata for the service handler to pick up — lets the
        # history record include schedule/trigger info even though the
        # service-call signature itself can't carry extras.
        entry_data = self._hass.data.get(DOMAIN, {}).get(self._entry_id)
        if entry_data is not None:
            entry_data.setdefault("pending_run_meta", {})[zone_id] = {
                "schedule_id": run.schedule_id,
                "schedule_name": run.schedule_name,
                "requested_minutes": run.duration_minutes,
                "triggers": triggers,
                "zone_name": zone_name,
            }

        _LOGGER.info(
            "Dispatching run: %s for %d min (schedule %s) — async; run_zone reports "
            "actual start/failure",
            zone_id,
            adjusted_minutes,
            run.schedule_name,
        )
        # Dispatch through run_zone. A run longer than the controller's
        # per-activation cap (Rachio ~60 min) is split into back-to-back blocks
        # inside handle_run_zone (v1.25) — the single funnel both scheduled and
        # manual runs share — so nothing controller-specific lives here.
        try:
            await self._hass.services.async_call(
                DOMAIN,
                "run_zone",
                {"entity_id": zone_id, "minutes": adjusted_minutes},
                blocking=False,
            )
        except Exception:
            _LOGGER.exception("Failed to fire scheduled run for %s", zone_id)
            # Clean up the stash so a later manual run doesn't accidentally
            # inherit the scheduled metadata.
            if entry_data is not None:
                entry_data.get("pending_run_meta", {}).pop(zone_id, None)

    # ── Per-gate evaluators (extracted from _fire_run in v1.16) ──

    def _zone_moisture_reading(self, zone_cfg: dict):
        """v1.19 — shared moisture read for the gate AND auto-soak.

        Applies the per-sensor analysis opt-out: entities listed in
        `moisture_excluded` stay bound for display but are filtered out
        before combining. Returns (combined_pct_or_None, analysis_ids,
        readings, excluded_ids)."""
        moisture_entities = zone_cfg.get("moisture_entities") or []
        excluded = set(zone_cfg.get("moisture_excluded") or [])
        analysis_ids = [e for e in moisture_entities if e not in excluded]
        readings: list[float] = []
        for ent in analysis_ids:
            state = self._hass.states.get(ent)
            if state is None or state.state in ("unknown", "unavailable"):
                continue
            try:
                readings.append(float(state.state))
            except (TypeError, ValueError):
                continue
        combine_mode = zone_cfg.get("combine_mode", COMBINE_AVERAGE)
        current = combine_moisture(readings, combine_mode)
        return current, analysis_ids, readings, sorted(excluded & set(moisture_entities))

    def _prune_stale_sessions(self, now: datetime) -> None:
        """Drop run sessions whose deadline (+ buffer + catch margin) has passed. An
        orphaned session — a chunked run interrupted before its final block never cleared
        its record — otherwise lingers and phantom-reserves the one-zone controller for
        its whole original window, silently blocking other zones (this is exactly how an
        app update that restarts HA mid-run could stop watering). Such a session is
        ALREADY ignored as a blocker (same threshold), so pruning changes no scheduling
        decision — it only stops orphans surviving across restarts, so the store self-heals.
        """
        sessions = self._config.get("active_run_sessions")
        if not sessions:
            return
        stale = stale_session_zones(sessions, now)
        if not stale:
            return
        for zone in stale:
            sessions.pop(zone, None)
        _LOGGER.info("Pruned %d finished run session(s) from the store: %s", len(stale), stale)
        self._hass.async_create_task(self.async_save_config())

    def _gap_trusted_sessions(self, sessions: dict) -> dict:
        """v1.39.1 — filter ``active_run_sessions`` for gap insertion: strip the
        recorded block plan (``blocks`` + ``block_gap_seconds``) from any session
        whose timers are NOT armed in this uptime — one whose ``started_at``
        predates ``_uptime_started_at`` (a pre-restart ghost: its block/auto-stop
        timers died with HA, so the plan's "gaps" may not be idle at all — the
        interrupted block's valve can still be physically open) or one marked
        ``resuming_gated`` (resume deferred; the valve is off waiting to retry,
        and the plan will be re-recorded fresh when the resume succeeds).

        The session itself is KEPT — it must still count as a busy span so a
        blocked short run stays deferred — only its gap windows become
        untrusted (no ``blocks`` -> ``session_gap_windows`` yields none: no
        insertion, fail safe). A successful resume re-enters run_zone, which
        re-records the session with a fresh ``started_at`` and re-armed
        timers, so insertion resumes automatically. Unparseable ``started_at``
        or tz mismatch -> untrusted (fail safe)."""
        if not isinstance(sessions, dict) or not sessions:
            return {}
        out: dict = {}
        for zone, s in sessions.items():
            if not isinstance(s, dict) or "blocks" not in s:
                out[zone] = s  # nothing to strip (plan-less sessions expose no gaps)
                continue
            trusted = not s.get("resuming_gated")
            if trusted:
                trusted = False  # prove this-uptime freshness or stay untrusted
                up = self._uptime_started_at
                try:
                    started = datetime.fromisoformat(str(s.get("started_at")))
                    if up is not None and (started.tzinfo is None) == (up.tzinfo is None):
                        trusted = started >= up
                except (TypeError, ValueError):
                    trusted = False
            if trusted:
                out[zone] = s
            else:
                out[zone] = {k: v for k, v in s.items() if k not in ("blocks", "block_gap_seconds")}
        return out

    # ── Auto-soak recovery (v1.19) ────────────────────────────────

    def _other_zone_busy(self, zone_id: str, now: datetime) -> bool:
        """True when a DIFFERENT zone has a live committed run (manual / scheduled /
        resumed) — i.e. the one-zone-at-a-time controller is busy elsewhere. The
        scheduled tick is already serialized by resolve_conflicts, but AUTO-SOAK fires
        outside that path, so it must consult this before opening a valve or it could
        drive a second zone into a running one (hydraulic collision on a single-zone
        controller). Reuses the same committed-blocker definition as the resolver —
        including the independent-supply exemption, so an independent zone's run neither
        counts as busy here nor is itself gated by one."""
        sessions = self._config.get("active_run_sessions") or {}
        blockers = committed_runs_from_sessions(
            sessions, now, independent_zones=self.independent_zones
        )
        return any(r.zone_entity_id != zone_id for r in blockers)

    async def _tick_auto_soak(self, now: datetime) -> None:
        """Closed-loop low-moisture recovery, driven once per tick.

        For each zone with auto_soak_enabled: when effective moisture
        (excluded sensors filtered out) drops below min_pct, run the
        zone for soak_run_minutes, wait soak_wait_minutes for the water
        to absorb, re-read, and repeat — until moisture >= min_pct or
        soak_max_cycles is exhausted (then notify + cooldown).
        """
        for zone_id, zone_cfg in (self._config.get("zones") or {}).items():
            if self.zone_is_locked(zone_id, now):
                # This zone's type is holding. Abandon only its sequence —
                # a turf hold must not stop a deep-drip or germination soak.
                if zone_id in self._soak_state:
                    _LOGGER.info(
                        "Auto-soak: rain lockout active for %s, abandoning its sequence",
                        zone_id,
                    )
                    self._soak_state.pop(zone_id, None)
                continue
            if not zone_cfg.get("auto_soak_enabled"):
                self._soak_state.pop(zone_id, None)
                continue
            if zone_cfg.get("moisture_disabled"):
                continue  # moisture is display-only for this zone

            state = self._soak_state.get(zone_id)
            if state is None:
                cooldown = self._soak_cooldown.get(zone_id)
                if cooldown and now < cooldown:
                    continue
                current, analysis_ids, _readings, _excl = self._zone_moisture_reading(zone_cfg)
                if current is None or not analysis_ids:
                    continue  # can't close the loop without a reading
                min_pct = float(zone_cfg.get("min_pct", DEFAULT_ZONE_MIN_PCT))
                if current >= min_pct:
                    continue
                zone_state = self._hass.states.get(zone_id)
                if zone_state is not None and zone_state.state == "on":
                    continue  # already watering (scheduled/manual) — let it finish
                if self._other_zone_busy(zone_id, now):
                    continue  # one-zone-at-a-time: another zone is watering
                await self._start_soak_run(zone_id, zone_cfg, now, cycle=1, current=current)
                continue

            if now < state["until"]:
                continue  # current phase still in progress

            if state["phase"] == "running":
                # Run finished (run_zone's auto-stop handles the valve);
                # let the water soak in before re-reading.
                wait_min = int(zone_cfg.get("soak_wait_minutes", _SOAK_DEFAULT_WAIT_MIN))
                state["phase"] = "soaking"
                state["until"] = now + timedelta(minutes=wait_min)
                continue

            # phase == "soaking" → soak finished, re-examine the sensors
            current, analysis_ids, _readings, _excl = self._zone_moisture_reading(zone_cfg)
            min_pct = float(zone_cfg.get("min_pct", DEFAULT_ZONE_MIN_PCT))
            if current is None or not analysis_ids:
                _LOGGER.info(
                    "Auto-soak %s: lost moisture reading mid-sequence, abandoning", zone_id
                )
                self._soak_state.pop(zone_id, None)
                continue
            if current >= min_pct:
                _LOGGER.info(
                    "Auto-soak %s: recovered to %.1f%% (min %.0f%%) after %d cycle(s)",
                    zone_id,
                    current,
                    min_pct,
                    state["cycle"],
                )
                self._soak_state.pop(zone_id, None)
                continue
            max_cycles = int(zone_cfg.get("soak_max_cycles", _SOAK_DEFAULT_MAX_CYCLES))
            if state["cycle"] >= max_cycles:
                self._soak_state.pop(zone_id, None)
                self._soak_cooldown[zone_id] = now + timedelta(minutes=_SOAK_GIVE_UP_COOLDOWN_MIN)
                zone_state = self._hass.states.get(zone_id)
                friendly = (
                    zone_state.attributes.get("friendly_name") if zone_state else None
                ) or zone_id
                _LOGGER.warning(
                    "Auto-soak %s: still %.1f%% (min %.0f%%) after %d cycles — cooldown %d min",
                    zone_id,
                    current,
                    min_pct,
                    max_cycles,
                    _SOAK_GIVE_UP_COOLDOWN_MIN,
                )
                if self.notifier is not None:
                    await self.notifier.notify(
                        f"Auto-soak couldn't raise {friendly} above its minimum "
                        f"({current:.0f}% after {max_cycles} run/soak cycles, min "
                        f"{min_pct:.0f}%). Check the moisture sensors and irrigation "
                        f"lines. Won't retry for {_SOAK_GIVE_UP_COOLDOWN_MIN // 60} hours.",
                        title="Irrigation: auto-soak gave up",
                        category=CATEGORY_IMPORTANT,
                        event_type="auto_soak_gave_up",
                    )
                continue
            zone_state = self._hass.states.get(zone_id)
            if zone_state is not None and zone_state.state == "on":
                continue  # a scheduled/manual run for this zone is live — let it finish
            if self._other_zone_busy(zone_id, now):
                continue  # one-zone-at-a-time: wait for the other zone to finish
            await self._start_soak_run(
                zone_id, zone_cfg, now, cycle=state["cycle"] + 1, current=current
            )

    async def _start_soak_run(
        self, zone_id: str, zone_cfg: dict, now: datetime, *, cycle: int, current: float
    ) -> None:
        """Fire one soak-cycle run via run_zone, labeled for History."""
        run_min = int(zone_cfg.get("soak_run_minutes", _SOAK_DEFAULT_RUN_MIN))
        max_cycles = int(zone_cfg.get("soak_max_cycles", _SOAK_DEFAULT_MAX_CYCLES))
        min_pct = float(zone_cfg.get("min_pct", DEFAULT_ZONE_MIN_PCT))
        entry_data = self._hass.data.get(DOMAIN, {}).get(self._entry_id)
        if entry_data is not None:
            # Label the run in History as an auto-soak cycle (the meta
            # stash is how scheduled runs carry context into run_zone).
            entry_data.setdefault("pending_run_meta", {})[zone_id] = {
                "schedule_id": None,
                "schedule_name": f"Auto-soak cycle {cycle}/{max_cycles}",
                "requested_minutes": run_min,
                "triggers": {
                    "auto_soak": {
                        "cycle": cycle,
                        "max_cycles": max_cycles,
                        "moisture_pct": current,
                        "min_pct": min_pct,
                    }
                },
                "zone_name": None,
            }
        self._soak_state[zone_id] = {
            "phase": "running",
            "cycle": cycle,
            "until": now + timedelta(minutes=run_min),
        }
        _LOGGER.info(
            "Auto-soak %s: cycle %d/%d — %.1f%% < min %.0f%%, running %d min",
            zone_id,
            cycle,
            max_cycles,
            current,
            min_pct,
            run_min,
        )
        try:
            await self._hass.services.async_call(
                DOMAIN,
                "run_zone",
                {"entity_id": zone_id, "minutes": run_min},
                blocking=False,
            )
        except Exception:
            _LOGGER.exception("Auto-soak %s: run_zone failed", zone_id)
            self._soak_state.pop(zone_id, None)

    def _evaluate_moisture_gate(
        self,
        zone_cfg: dict,
        zone_id: str,
        adjusted_minutes: int,
        triggers: dict,
    ) -> tuple[int, str | None]:
        """Read moisture sensors, populate triggers["moisture"], return
        (possibly-adjusted minutes, skip reason or None).

        No moisture binding → no-op. Multiple sensors → combined via
        the zone's `combine_mode`. The evaluate_moisture decision either
        adjusts the runtime (boost / cut) or returns a skip reason."""
        moisture_entities = zone_cfg.get("moisture_entities") or []
        if not moisture_entities:
            return adjusted_minutes, None

        # v1.18.4 — per-zone moisture opt-out. Sensors stay bound for
        # display, but the gate is a no-op: no saturated-skip, no
        # runtime adjustment, and require_moisture_reading is moot.
        # Recorded in triggers so History shows the gate was
        # deliberately bypassed rather than silently absent.
        if zone_cfg.get("moisture_disabled"):
            triggers["moisture"] = {"disabled_by_config": True}
            return adjusted_minutes, None

        current, analysis_ids, readings, excluded_ids = self._zone_moisture_reading(zone_cfg)
        # v1.19 — every bound sensor excluded from analysis ⇒ nothing to
        # decide with. Treat like "no moisture binding" (no-op, even if
        # require_moisture_reading is on — the user excluded them all on
        # purpose) but leave a breadcrumb in triggers.
        if not analysis_ids:
            triggers["moisture"] = {"all_sensors_excluded": True, "excluded": excluded_ids}
            return adjusted_minutes, None
        min_pct = float(zone_cfg.get("min_pct", DEFAULT_ZONE_MIN_PCT))
        target_pct = float(zone_cfg.get("target_pct", DEFAULT_ZONE_TARGET_PCT))
        max_pct = float(zone_cfg.get("max_pct", DEFAULT_ZONE_MAX_PCT))
        triggers["moisture"] = {
            "entities": list(analysis_ids),
            "excluded": excluded_ids,
            "readings": readings,
            "combined_pct": current,
            "combine_mode": zone_cfg.get("combine_mode", COMBINE_AVERAGE),
            "min_pct": min_pct,
            "target_pct": target_pct,
            "max_pct": max_pct,
            "decision": "no_reading" if current is None else "ok",
        }
        if current is None:
            # v1.18 — fail-closed option. When require_moisture_reading is
            # set on this zone and NO sensor produced a value (all offline
            # / unavailable / non-numeric), skip the run rather than
            # watering blind. Default off → legacy fail-open behavior
            # (water anyway when sensors are dark).
            if zone_cfg.get("require_moisture_reading"):
                triggers["moisture"]["decision"] = "skip_no_reading"
                return adjusted_minutes, (
                    "no moisture reading available and require_moisture_reading is on "
                    f"(sensors: {', '.join(analysis_ids)})"
                )
            return adjusted_minutes, None
        decision = evaluate_moisture(
            current=current,
            min_pct=min_pct,
            target=target_pct,
            max_pct=max_pct,
            base_minutes=adjusted_minutes,
        )
        _LOGGER.info("Moisture decision for %s: %s", zone_id, decision.reason)
        if decision.skip:
            triggers["moisture"]["decision"] = "skip"
            return adjusted_minutes, decision.reason
        triggers["moisture"]["decision"] = "ok"
        triggers["moisture"]["adjusted_minutes"] = decision.runtime_minutes
        return decision.runtime_minutes, None

    def _evaluate_wind_gate(self, triggers: dict) -> str | None:
        """PRD #52 — defer if current wind ≥ threshold. Threshold of 0
        or missing → off, returns None without touching triggers."""
        wind_threshold = self._config.get("wind_defer_mph", 0)
        if not wind_threshold or float(wind_threshold) <= 0:
            return None
        wind = self._read_current_wind_mph()
        deferred = evaluate_wind_defer(wind, float(wind_threshold))
        triggers["wind"] = {
            "mph": wind,
            "threshold_mph": float(wind_threshold),
            "deferred": deferred,
        }
        if deferred:
            return f"wind defer (current {wind} mph >= {wind_threshold} mph)"
        return None

    def _evaluate_hot_weather_gate(
        self, adjusted_minutes: int, triggers: dict
    ) -> tuple[int, str | None]:
        """Hot-weather runtime boost. Returns (new minutes, boost-note
        for logging or None). No threshold configured → no-op."""
        forecast_high = self._read_forecast_high()
        threshold = self._config.get("hot_threshold_f")
        boost_pct = self._config.get("boost_percent", 0)
        if forecast_high is None or threshold is None:
            return adjusted_minutes, None
        hw = evaluate_hot_weather(
            daily_high_f=forecast_high,
            threshold_f=float(threshold),
            boost_percent=int(boost_pct),
        )
        triggers["hot_weather"] = {
            "forecast_high_f": forecast_high,
            "threshold_f": float(threshold),
            "boost_percent": int(boost_pct),
            "applied_multiplier": hw.multiplier,
            "boost_applied": bool(hw.boost),
        }
        if not hw.boost:
            return adjusted_minutes, None
        # Clamp to the same absolute ceiling the service boundary enforces. Without this
        # a long base run x boost can exceed MAX_SCHEDULE_DURATION_MIN; because _fire_run
        # dispatches run_zone with blocking=False, the boundary's ValueError would be
        # raised inside a fire-and-forget task (not caught here) and the run would vanish
        # silently on the hottest days.
        new_minutes = min(
            MAX_SCHEDULE_DURATION_MIN, max(1, round(adjusted_minutes * hw.multiplier))
        )
        note = f"hot-weather boost (high {forecast_high}F >= {threshold}F) x{hw.multiplier:.2f}"
        return new_minutes, note

    # ── Rain lockout ──────────────────────────────────────────────

    def _lockout_state(self) -> LockoutState:
        return LockoutState(
            lockout_until=dict(self._lockouts),
            event_rain_in=self._event_rain_in,
            event_anchor=self._event_anchor,
            released_zones=set(self._released_zones),
            forecast_extended_on=dict(self._forecast_extended_on),
            rain_source_disagree=self._rain_source_disagree,
            last_tempest_in=self._last_tempest_in,
            last_open_meteo_in=self._last_open_meteo_in,
            last_combined_in=self._last_combined_in,
            rain_source=self._rain_source,
        )

    def _restore_lockout_state(self, state: LockoutState) -> None:
        self._lockouts = dict(state.lockout_until)
        self._event_rain_in = state.event_rain_in
        self._event_anchor = state.event_anchor
        self._released_zones = set(state.released_zones)
        self._forecast_extended_on = dict(state.forecast_extended_on)
        self._rain_source_disagree = state.rain_source_disagree
        self._last_tempest_in = state.last_tempest_in
        self._last_open_meteo_in = state.last_open_meteo_in
        self._last_combined_in = state.last_combined_in
        self._rain_source = state.rain_source

    def lockout_attributes(self) -> dict[str, Any]:
        """Binary-sensor attributes. ``until`` stays the latest deadline."""
        until = self.lockout_until
        return {
            "until": until.isoformat() if until else None,
            "lockout_until": {
                zone_type: moment.isoformat()
                for zone_type, moment in sorted(self._lockouts.items())
            },
            "released_zones": sorted(self._released_zones),
            "active_rain": self._active_rain,
            "rain_inches_72h": self._last_combined_in,
            "tempest_inches_72h": self._last_tempest_in,
            "open_meteo_inches_72h": self._last_open_meteo_in,
            "rain_source": self._rain_source,
            RAIN_SOURCE_DISAGREE: self._rain_source_disagree,
        }

    def _publish_lockout(self) -> None:
        """Rewrite the binary sensor when the on/off answer changes, and arm
        a point-in-time callback at the next deadline so expiry does not wait
        for the following tick. The sensor does not poll."""
        on = self.is_locked_out_now()
        if on != self._lockout_published_on:
            self._lockout_published_on = on
            self._notify_lockout_change()
        self._arm_lockout_expiry()

    def _arm_lockout_expiry(self) -> None:
        cancel = self._cancel_lockout_expiry
        if cancel is not None:
            try:
                cancel()
            except Exception:
                _LOGGER.debug("lockout expiry cancel failed", exc_info=True)
            self._cancel_lockout_expiry = None
        nxt = self.lockout_until
        if nxt is None:
            return
        try:
            from homeassistant.core import callback
            from homeassistant.helpers.event import async_track_point_in_time

            @callback
            def _fire(moment):
                self._cancel_lockout_expiry = None
                self._publish_lockout()

            self._cancel_lockout_expiry = async_track_point_in_time(self._hass, _fire, nxt)
        except Exception:
            _LOGGER.debug("could not arm rain-lockout expiry", exc_info=True)

    def _rain_floor_inches(self) -> float:
        from .weather_gate import RAIN_LOCKOUT_MIN_INCHES

        try:
            floor = float(self._config.get("rain_lockout_min_inches", RAIN_LOCKOUT_MIN_INCHES))
        except (TypeError, ValueError):
            floor = RAIN_LOCKOUT_MIN_INCHES
        if not math.isfinite(floor) or floor <= 0:
            floor = RAIN_LOCKOUT_MIN_INCHES
        # The day table itself starts at 0.10 in. A higher user floor (the
        # monsoon knob) still wins; a lower one cannot unlock a trace.
        return max(floor, RAIN_LOCKOUT_MIN_INCHES)

    def _current_daily_eto(self) -> float | None:
        try:
            week = float(self.eto_status().get("eto_in_week"))
        except (TypeError, ValueError, AttributeError):
            return None
        if math.isfinite(week) and week > 0:
            return week / 7.0
        return None

    def classify_zone(self, zone_id: str, schedule=None) -> str:
        """Zone type from config, plants, and schedule metadata already stored."""
        friendly = None
        try:
            state = self._hass.states.get(zone_id) if self._hass is not None else None
        except Exception:
            state = None
        if state is not None:
            friendly = state.attributes.get("friendly_name")
        cfg = (self._config.get("zones") or {}).get(zone_id) or {}
        presets: list[str] = []
        try:
            presets = [
                plant.care_plan_preset
                for plant in self._plants.by_zone(zone_id)
                if plant.care_plan_preset
            ]
        except Exception:
            presets = []
        created_via = schedule.created_via if schedule is not None else None
        if created_via != "establishment" and self._zone_is_germinating(zone_id):
            created_via = "establishment"
        schedule_name = schedule.name if schedule is not None else None
        profile = schedule.split_profile if schedule is not None else None
        return classify_zone_type(
            zone_entity_id=zone_id,
            friendly_name=friendly,
            category=cfg.get("category"),
            split_profile=profile,
            plant_presets=presets,
            independent=zone_id in self.independent_zones,
            schedule_name=schedule_name,
            created_via=created_via,
        )

    def _zone_is_germinating(self, zone_id: str) -> bool:
        """True while an establishment schedule for this zone is still active."""
        from homeassistant.util import dt as dt_util

        today = dt_util.now().date()
        for sched in self._store.all():
            if not sched.enabled or sched.created_via != "establishment":
                continue
            zones = {step.zone_entity_id for step in sched.all_steps()}
            if zone_id not in zones:
                continue
            if sched.end_date is not None and sched.end_date < today:
                continue
            return True
        return False

    def _any_zone_of_type(self, zone_type: str) -> bool:
        seen: set[str] = set()
        for sched in self._store.all():
            for step in sched.all_steps():
                seen.add(step.zone_entity_id)
        for zone_id in list(self._config.get("zones") or {}) + list(self.independent_zones):
            seen.add(zone_id)
        return any(self.classify_zone(zone_id) == zone_type for zone_id in seen)

    def zone_is_locked(self, zone_id: str, now: datetime, schedule=None) -> bool:
        """Whether ``zone_id`` should skip watering at ``now``.

        Germination never locks. Birdbath locks only during active rain.
        A completed run or a wet moisture reading releases that zone only;
        other zones of the same type stay held until ``lockout_until``.
        """
        if schedule is not None and schedule.ignore_rain_lockout:
            return False
        zone_type = self.classify_zone(zone_id, schedule)
        if zone_type == TYPE_GERMINATION:
            return False
        if zone_type == TYPE_BIRDBATH:
            return self._active_rain
        if zone_id in self._released_zones:
            return False
        until = self._lockouts.get(zone_type)
        return until is not None and now < until

    def schedule_is_locked(self, schedule, now: datetime) -> bool:
        if schedule.ignore_rain_lockout:
            return False
        return any(
            self.zone_is_locked(step.zone_entity_id, now, schedule) for step in schedule.all_steps()
        )

    def note_zone_run_completed(self, zone_id: str) -> None:
        """A finished run ends that zone's hold. It does not clear the type
        for zones that have not run, and it never moves a deadline later."""
        zone_type = self.classify_zone(zone_id)
        if zone_type not in DURABLE_ZONE_TYPES:
            return
        until = self._lockouts.get(zone_type)
        if until is None or zone_id in self._released_zones:
            return
        self._released_zones.add(zone_id)
        _LOGGER.info("Rain lockout released for %s after a completed run", zone_id)
        self._publish_lockout()
        self._spawn(self.async_save_lockout())

    def extend_lockout(self, hours: float, zone_type: str | None = None) -> None:
        """Add ``hours`` to a hold. ``zone_type`` None or ``all`` covers every
        durable type. A type that is not currently holding starts from now.
        Manual extends are not clipped by the heat ceiling — the caller asked
        for this long. Does not write schedules.
        """
        from homeassistant.util import dt as dt_util

        if hours <= 0:
            return
        now = dt_util.now()
        if zone_type in (None, "", "all"):
            types = DURABLE_ZONE_TYPES
        elif zone_type in DURABLE_ZONE_TYPES:
            types = (zone_type,)
        else:
            raise ValueError(f"unknown zone_type {zone_type!r}")
        for kind in types:
            current = self._lockouts.get(kind)
            base = current if current is not None and current > now else now
            self._lockouts[kind] = base + timedelta(hours=hours)
        _LOGGER.info("Rain lockout extended by %sh for %s", hours, ",".join(types))
        self._publish_lockout()
        self._spawn(self.async_save_lockout())

    async def _record_lockout_skips(
        self, schedules, last: datetime, now: datetime, zone_buffer
    ) -> None:
        """Log occurrences skipped because their zone type is holding."""
        upcoming = next_runs(
            schedules,
            from_dt=last,
            until_dt=now + timedelta(seconds=1),
            zone_buffer_seconds=int(zone_buffer) if zone_buffer is not None else None,
            sun_times=self.sun_times,
        )
        due = due_runs_since(upcoming, last, now)
        if not due:
            return
        for run in due:
            zone_state = self._hass.states.get(run.zone_entity_id)
            zone_name = (
                zone_state.attributes.get("friendly_name") if zone_state else None
            ) or run.zone_entity_id
            zone_type = self.classify_zone(run.zone_entity_id)
            until = self._lockouts.get(zone_type)
            self._run_history.record_skipped(
                zone_entity_id=run.zone_entity_id,
                zone_name=zone_name,
                requested_minutes=run.duration_minutes,
                schedule_id=run.schedule_id,
                schedule_name=run.schedule_name,
                fired_at=now,
                reason="rain lockout",
                triggers={
                    "rain_lockout": {
                        "zone_type": zone_type,
                        "until": until.isoformat() if until else None,
                        "rain_inches_72h": self._last_combined_in,
                        "active_rain": self._active_rain,
                    }
                },
            )
            _LOGGER.info(
                "Skipped %s (%s) — rain lockout (%s)",
                run.zone_entity_id,
                run.schedule_name,
                zone_type,
            )
        try:
            await self.async_save_run_history()
        except Exception:
            _LOGGER.exception("Saving rain-lockout skip history failed")

    async def _evaluate_lockout(self, now: datetime) -> None:
        """Refresh the 72h rain total and per-type holds.

        Always republishes sensor state, including the path where no rain
        sensor is configured, so a deadline that passed between ticks turns
        the binary sensor off.
        """
        was_holding = self.is_locked_out_now()
        try:
            await self._evaluate_lockout_body(now)
        except Exception:
            _LOGGER.exception("Rain lockout evaluation failed")
        finally:
            self._publish_lockout()
        if not was_holding and self.is_locked_out_now() and self.notifier:
            until = self.lockout_until
            inches = self._last_combined_in
            detail = f"{inches:.2f} in over 72h" if inches is not None else "rain"
            await self.notifier.notify(
                f"Rain lockout active ({detail}) — held zone types pause until "
                f"{until.isoformat() if until else 'the storm passes'}",
                title="Rain lockout active",
                category=CATEGORY_IMPORTANT,
                event_type="rain_lockout_started",
                extra=self.lockout_attributes(),
            )

    async def _evaluate_lockout_body(self, now: datetime) -> None:
        tempest = await self._read_tempest_72h(now)
        meteo = await self._read_open_meteo_rain(now)
        combined = combine_rain_totals(
            None if tempest is None else tempest[0],
            None if meteo is None else meteo.get("precip_72h_in"),
            tempest_last_rain_at=None if tempest is None else tempest[1],
            meteo_last_rain_at=None if meteo is None else meteo.get("last_rain_at"),
        )
        self._remember_rain_sources(tempest, meteo, combined)
        if meteo and meteo.get("forecast_high_f") is not None:
            self._forecast_high_f = meteo["forecast_high_f"]
            self._forecast_high_at = now
        self._active_rain = self._detect_active_rain(meteo)
        changed = False
        changed |= self._arm_lockout_from_rain(now, combined, meteo)
        changed |= self._maybe_extend_for_forecast(now, meteo)
        changed |= self._apply_moisture_releases()
        self._sync_rain_disagree_issue(combined)
        if changed:
            await self.async_save_lockout()

    def _remember_rain_sources(self, tempest, meteo, combined: CombinedRain) -> None:
        self._last_tempest_in = combined.tempest_in
        self._last_open_meteo_in = combined.meteo_in
        self._last_combined_in = combined.inches
        self._rain_source = combined.source
        self._rain_source_disagree = combined.disagree
        if combined.source == "open_meteo" and "tempest" not in self._logged_source_fallback:
            self._logged_source_fallback.add("tempest")
            _LOGGER.info(
                "Rain lockout: Tempest 72h total unavailable; using Open-Meteo (%.2f in)",
                combined.inches or 0.0,
            )
        elif combined.source == "tempest" and "open_meteo" not in self._logged_source_fallback:
            self._logged_source_fallback.add("open_meteo")
            _LOGGER.info(
                "Rain lockout: Open-Meteo 72h total unavailable; using Tempest (%.2f in)",
                combined.inches or 0.0,
            )
        elif combined.source == "none" and "both" not in self._logged_source_fallback:
            self._logged_source_fallback.add("both")
            _LOGGER.info("Rain lockout: neither Tempest nor Open-Meteo has a 72h total")
        if combined.source not in ("tempest", "none"):
            self._logged_source_fallback.discard("open_meteo")
        if combined.source not in ("open_meteo", "none"):
            self._logged_source_fallback.discard("tempest")
        if combined.source != "none":
            self._logged_source_fallback.discard("both")

    def _detect_active_rain(self, meteo: dict | None) -> bool:
        conditions: list[str] = []
        try:
            entity_ids = self._hass.states.async_entity_ids("weather")
            if isinstance(entity_ids, (list, tuple)):
                for entity_id in entity_ids[:1]:
                    state = self._hass.states.get(entity_id)
                    if state is not None:
                        conditions.append(str(state.state))
        except Exception:
            conditions = []
        rate = None
        # A precipitation-rate sensor, when the user configured one, is a
        # direct "it is raining" signal. The daily accumulation is not.
        rate_entity = self._config.get("rain_rate_sensor")
        if rate_entity:
            state = self._hass.states.get(rate_entity)
            if state is not None:
                rate = rain_to_inches(state.state, state.attributes.get("unit_of_measurement"))
        hour_mm = None if meteo is None else meteo.get("current_hour_precip_mm")
        return active_rain(conditions, precip_rate_in_per_h=rate, current_hour_precip_mm=hour_mm)

    def _arm_lockout_from_rain(
        self, now: datetime, combined: CombinedRain, meteo: dict | None
    ) -> bool:
        """Set per-type deadlines from the 72h total. Only extends.

        The anchor is when the rain fell (``last_rain_at``), not this tick, so
        a daily total that stays elevated cannot pin the hold. A total that
        has not grown past the event that armed the hold is ignored.
        """
        if combined.inches is None:
            return False
        if combined.inches < self._rain_floor_inches():
            return False
        previous = self._event_rain_in
        if previous is not None and combined.inches <= previous + SAME_EVENT_EPSILON_IN:
            return False
        anchor = combined.last_rain_at or self._event_anchor or now
        daily_eto = select_lockout_daily_eto(
            self._current_daily_eto(),
            None if meteo is None else meteo.get("forecast_daily_eto_in"),
        )
        temp_f = self._read_heat_signal_f()
        changed = False
        revoked: set[str] = set()
        for zone_type in DURABLE_ZONE_TYPES:
            hours = scaled_lockout_hours(combined.inches, zone_type, daily_eto, temp_f)
            if hours is None:
                continue
            new_until = anchor + timedelta(hours=hours)
            if new_until <= now:
                continue
            old = self._lockouts.get(zone_type)
            if old is None or new_until > old:
                self._lockouts[zone_type] = new_until
                revoked.add(zone_type)
                changed = True
                _LOGGER.info(
                    "Rain lockout %s until %s (%.2f in over 72h, %dh, anchor %s)",
                    zone_type,
                    new_until.isoformat(),
                    combined.inches,
                    hours,
                    anchor.isoformat(),
                )
        if not changed and previous is None:
            # Rain is real but every deadline is already in the past. Remember
            # the total so the next tick does not re-evaluate it as a new event.
            self._event_rain_in = combined.inches
            self._event_anchor = anchor
            return True
        if changed:
            self._event_rain_in = combined.inches
            self._event_anchor = anchor
            if revoked:
                self._released_zones = {
                    zone for zone in self._released_zones if self.classify_zone(zone) not in revoked
                }
        return changed

    def _maybe_extend_for_forecast(self, now: datetime, meteo: dict | None) -> bool:
        """Add one day to holds that are already active, once per local day.

        Clipped by the heat ceiling measured from ``now``, so a hot forecast
        cannot push a hold past the same cap a fresh rain event would hit.
        Does not start a hold that rain itself did not start.
        """
        if not meteo or not meteo.get("forecast_extends"):
            return False
        from .weather_gate import rain_lockout_max_hours

        day = now.date().isoformat()
        ceiling = now + timedelta(hours=rain_lockout_max_hours(self._read_heat_signal_f()))
        changed = False
        for zone_type in DURABLE_ZONE_TYPES:
            until = self._lockouts.get(zone_type)
            if until is None or until <= now:
                continue
            if self._forecast_extended_on.get(zone_type) == day:
                continue
            extended = min(until + timedelta(days=1), ceiling)
            self._forecast_extended_on[zone_type] = day
            changed = True
            if extended > until:
                self._lockouts[zone_type] = extended
                _LOGGER.info(
                    "Rain lockout %s extended 1 day for forecast rain (until %s)",
                    zone_type,
                    extended.isoformat(),
                )
        return changed

    def _apply_moisture_releases(self) -> bool:
        """Wet soil releases that zone early. A dry reading changes nothing."""
        changed = False
        for zone_id, cfg in (self._config.get("zones") or {}).items():
            if zone_id in self._released_zones or cfg.get("moisture_disabled"):
                continue
            zone_type = self.classify_zone(zone_id)
            until = self._lockouts.get(zone_type)
            if zone_type not in DURABLE_ZONE_TYPES or until is None:
                continue
            if not cfg.get("moisture_entities"):
                continue
            try:
                current, analysis_ids, _readings, _excl = self._zone_moisture_reading(cfg)
            except Exception:
                continue
            if not analysis_ids:
                continue
            target = float(cfg.get("target_pct", DEFAULT_ZONE_TARGET_PCT))
            if moisture_releases_lockout(current, target):
                self._released_zones.add(zone_id)
                changed = True
                _LOGGER.info(
                    "Rain lockout released early for %s — moisture %.0f%% >= target %.0f%%",
                    zone_id,
                    current if current is not None else -1,
                    target,
                )
        return changed

    def _sync_rain_disagree_issue(self, combined: CombinedRain) -> None:
        try:
            from homeassistant.helpers import issue_registry as ir
        except Exception:
            return
        issue_id = "rain_source_disagree"
        try:
            if combined.disagree:
                ir.async_create_issue(
                    self._hass,
                    DOMAIN,
                    issue_id,
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="rain_source_disagree",
                    translation_placeholders={
                        "tempest": _fmt_in(combined.tempest_in),
                        "meteo": _fmt_in(combined.meteo_in),
                        "used": _fmt_in(combined.inches),
                    },
                )
            else:
                ir.async_delete_issue(self._hass, DOMAIN, issue_id)
        except Exception:
            _LOGGER.debug("rain-source repair issue update failed", exc_info=True)

    async def _read_tempest_72h(self, now: datetime) -> tuple[float, datetime | None] | None:
        """72h sum of the configured daily rain accumulator, in inches.

        Recorder history supplies the prior days. If history is unavailable
        the live reading is still used (today only) and the miss is logged
        once — the other source then carries the rest of the window.
        """
        entity_id = self._config.get("rain_sensor")
        if not entity_id:
            return None
        state = self._hass.states.get(entity_id)
        unit = None
        current = None
        if state is not None and state.state not in ("unknown", "unavailable", None):
            unit = state.attributes.get("unit_of_measurement")
            current = rain_to_inches(state.state, unit)
        samples = await self._tempest_history_samples(entity_id, now, unit)
        if current is not None:
            samples.append((now, current))
        total, last_rain_at = sum_daily_peaks(samples, now)
        if total is None:
            return None
        return total, last_rain_at

    async def _tempest_history_samples(
        self, entity_id: str, now: datetime, unit: str | None
    ) -> list[tuple[datetime, float]]:
        try:
            from homeassistant.components.recorder import get_instance, history
        except Exception:
            self._log_history_unavailable("recorder not installed")
            return []
        start = now - timedelta(hours=72)

        def _query():
            return history.state_changes_during_period(
                self._hass, start, now, entity_id, no_attributes=False
            )

        try:
            instance = get_instance(self._hass)
            raw = await instance.async_add_executor_job(_query)
        except Exception as err:
            self._log_history_unavailable(err)
            return []
        states = []
        if isinstance(raw, dict):
            states = raw.get(entity_id) or []
        elif isinstance(raw, list):
            states = raw
        samples: list[tuple[datetime, float]] = []
        for item in states:
            moment = getattr(item, "last_changed", None) or getattr(item, "last_updated", None)
            if moment is None:
                continue
            value = rain_to_inches(getattr(item, "state", None), unit)
            if value is None:
                continue
            samples.append((moment, value))
        return samples

    def _log_history_unavailable(self, err: object) -> None:
        if "history" in self._logged_source_fallback:
            return
        self._logged_source_fallback.add("history")
        _LOGGER.info("Rain lockout: Tempest history unavailable (%s); using the live reading", err)

    async def _read_open_meteo_rain(self, now: datetime) -> dict | None:
        """Cached Open-Meteo past_days=3 fetch. None when it cannot be reached."""
        from homeassistant.util import dt as dt_util

        cached_at = self._meteo_rain_cached_at
        if (
            self._meteo_rain_cache is not None
            and cached_at is not None
            and (now - cached_at).total_seconds() < 15 * 60
        ):
            return self._meteo_rain_cache
        lat = _plain_float(getattr(self._hass.config, "latitude", None))
        lon = _plain_float(getattr(self._hass.config, "longitude", None))
        if lat is None or lon is None:
            self._log_history_unavailable_meteo("no Home Assistant latitude/longitude")
            return None
        tz = getattr(self._hass.config, "time_zone", None)
        if not isinstance(tz, str):
            tz = None
        import json as _json

        import aiohttp
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        from .open_meteo import open_meteo_rain_url

        try:
            session = async_get_clientsession(self._hass)
            async with session.get(
                open_meteo_rain_url(lat, lon, tz),
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    self._log_history_unavailable_meteo(f"HTTP {resp.status}")
                    return None
                raw = await resp.content.read(400_000)
            parsed = parse_open_meteo_rain(_json.loads(raw), dt_util.as_local(now))
        except Exception as err:
            self._log_history_unavailable_meteo(err)
            return None
        self._meteo_rain_cache = parsed
        self._meteo_rain_cached_at = now
        return parsed

    def _log_history_unavailable_meteo(self, err: object) -> None:
        if "meteo_fetch" in self._logged_source_fallback:
            return
        self._logged_source_fallback.add("meteo_fetch")
        _LOGGER.info("Rain lockout: Open-Meteo fetch unavailable (%s)", err)

    # ── Weather helpers ───────────────────────────────────────────

    def _read_forecast_high(self) -> float | None:
        ent = self._config.get("temperature_sensor")
        if not ent:
            return None
        state = self._hass.states.get(ent)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    def _read_heat_signal_f(self) -> float | None:
        """Hottest relevant temperature (F) for the rain-lockout ceiling.

        The ceiling reflects the heat the plants will face DURING the lockout, so
        take the MAX of three signals, each normalized to Fahrenheit:
          (a) the configured temperature sensor,
          (b) the current temperature from a weather.* entity,
          (c) the cached near-term forecast daily HIGH (today+tomorrow) — this is
              what makes the ceiling respond to a hot FORECAST even when the rain
              itself falls during a cool storm cell.
        The hottest wins (a shorter ceiling errs toward watering). Returns None
        when no signal is available (-> the fail-safe 24h cap)."""
        vals: list[float] = []
        # (a) configured temperature sensor, unit-converted
        ent = self._config.get("temperature_sensor")
        if ent:
            state = self._hass.states.get(ent)
            if state is not None and state.state not in ("unknown", "unavailable"):
                f = temp_to_fahrenheit(state.state, state.attributes.get("unit_of_measurement"))
                if f is not None:
                    vals.append(f)
        # (b) current temp from the first usable weather.* entity, unit-converted
        for eid in self._hass.states.async_entity_ids("weather"):
            state = self._hass.states.get(eid)
            if state is None or state.state in ("unknown", "unavailable"):
                continue
            f = temp_to_fahrenheit(
                state.attributes.get("temperature"),
                state.attributes.get("temperature_unit"),
            )
            if f is not None:
                vals.append(f)
            break  # first usable weather entity is enough
        # (c) cached forecast daily high, if fresh (<=36h, matching the ETo gate)
        if self._forecast_high_f is not None and self._forecast_high_at is not None:
            from homeassistant.util import dt as dt_util

            age_h = (dt_util.now() - self._forecast_high_at).total_seconds() / 3600
            if age_h <= 36:
                vals.append(self._forecast_high_f)
        return max(vals) if vals else None

    def _read_current_wind_mph(self) -> float | None:
        """PRD #52 — current wind speed (mph) from configured sensor.

        Prefers the explicit `wind_sensor` config key; falls back to any
        weather.* entity's `wind_speed` attribute. Returns None when no
        wind data is available."""
        ent = self._config.get("wind_sensor")
        if ent:
            state = self._hass.states.get(ent)
            if state is not None and state.state not in ("unknown", "unavailable"):
                try:
                    return float(state.state)
                except (TypeError, ValueError):
                    pass
        # Fallback: scan for a weather.* entity with a wind_speed attr
        for eid in self._hass.states.async_entity_ids("weather"):
            state = self._hass.states.get(eid)
            if not state:
                continue
            wind = state.attributes.get("wind_speed")
            if wind is None:
                continue
            try:
                return float(wind)
            except (TypeError, ValueError):
                continue
        return None

    # ── Auto ETo (FAO-56 from the weather forecast) ───────────────

    def _resolve_weather_entity(self) -> str | None:
        """The weather entity to pull a forecast from: the configured
        `weather_entity`, else the first available weather.* entity."""
        ent = self._config.get("weather_entity")
        if ent:
            return ent
        ids = self._hass.states.async_entity_ids("weather")
        return ids[0] if ids else None

    def _eto_decision(self) -> tuple[float, str]:
        """(value, source) for the reference ETo — auto when enabled+fresh,
        else the manual figure. Pure policy lives in et_calc.select_eto."""
        from homeassistant.util import dt as dt_util

        age_h = None
        if self._auto_eto_at is not None:
            age_h = (dt_util.now() - self._auto_eto_at).total_seconds() / 3600
        return select_eto(
            manual=self._config.get("eto_in_week"),
            auto_value=self._auto_eto_in_week,
            auto_enabled=bool(self._config.get("eto_auto")),
            auto_age_hours=age_h,
            default=DEFAULT_ETO_IN_WEEK,
        )

    def effective_eto(self) -> float:
        """Reference ETo (in/week) the design math should use right now. Always
        a positive number — auto (FAO-56 from weather) when enabled and fresh,
        otherwise the manual eto_in_week (or the default). Never raises."""
        return self._eto_decision()[0]

    def eto_status(self) -> dict[str, Any]:
        """Snapshot of the ETo source + values for the Yard UI / health feed."""
        value, source = self._eto_decision()
        manual = self._config.get("eto_in_week")
        try:
            manual = float(manual) if manual is not None else DEFAULT_ETO_IN_WEEK
        except (TypeError, ValueError):
            manual = DEFAULT_ETO_IN_WEEK
        return {
            "eto_in_week": value,  # what the math uses (back-compat key)
            "eto_source": source,
            "eto_auto": bool(self._config.get("eto_auto")),
            "eto_manual": manual,
            "eto_auto_value": self._auto_eto_in_week,
            "eto_auto_at": self._auto_eto_at.isoformat() if self._auto_eto_at else None,
            "weather_entity": self._resolve_weather_entity(),
            "eto_provider": str(self._config.get("eto_provider") or "ha"),  # v1.49
        }

    def _zone_moisture_band(self, zone_id: str) -> str:
        """Current moisture band for a zone (a moisture_gate BAND_* string), or ""
        when there's no usable reading. Read-only — reuses the same reading +
        evaluate_moisture machinery as the watering gate, for the advisory plan."""
        zone_cfg = (self._config.get("zones") or {}).get(zone_id) or {}
        if not zone_cfg.get("moisture_entities") or zone_cfg.get("moisture_disabled"):
            return ""
        current, analysis_ids, _readings, _excl = self._zone_moisture_reading(zone_cfg)
        if not analysis_ids or current is None:
            return ""
        return evaluate_moisture(
            current=current,
            min_pct=float(zone_cfg.get("min_pct", DEFAULT_ZONE_MIN_PCT)),
            target=float(zone_cfg.get("target_pct", DEFAULT_ZONE_TARGET_PCT)),
            max_pct=float(zone_cfg.get("max_pct", DEFAULT_ZONE_MAX_PCT)),
            base_minutes=10,
        ).band

    def build_today_plan(self, now=None):
        """v1.32 — the advisory daily plan: today's planned runs prioritized by
        urgency (weekly water deficit + ET demand + moisture band), with skip
        recommendations for saturated zones. ADVISORY ONLY — it never changes what
        fires; it's surfaced via health.json, the panel, and the LLM advisor. Never
        raises (returns an empty plan on any data gap)."""
        from homeassistant.util import dt as dt_util

        from .daily_planner import DailyPlan, ZoneSignal, build_daily_plan
        from .hydraulics import DEFAULT_EFFICIENCY
        from .plant_design import build_yard_report

        try:
            now = now or dt_util.now()
            start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
            end_of_day = start_of_day + timedelta(days=1)
            zb = self._config.get("zone_buffer_seconds")
            planned = next_runs(
                self._store.enabled_schedules(),
                from_dt=start_of_day,
                until_dt=end_of_day,
                zone_buffer_seconds=int(zb) if zb is not None else None,
                sun_times=self.sun_times,
            )
            eto = float(self.effective_eto())
            eto_factor = max(0.0, min(1.0, eto / SANE_ETO_MAX)) if SANE_ETO_MAX else 0.0
            efficiency = float(self._config.get("drip_efficiency", DEFAULT_EFFICIENCY))
            # Per-zone weekly water-deficit fraction, from the hydraulics yard report.
            deficit_frac: dict[str, float] = {}
            for rep in build_yard_report(self.plants.all(), self._store.all(), eto, efficiency):
                need = sum(p.need_gal_week for p in rep.plants)
                delivered = sum(p.delivered_gal_week for p in rep.plants)
                deficit_frac[rep.loop.id] = max(0.0, (need - delivered) / need) if need > 0 else 0.0
            signals: dict[str, ZoneSignal] = {}
            zone_names: dict[str, str] = {}
            for r in planned:
                z = r.zone_entity_id
                if z not in signals:
                    signals[z] = ZoneSignal(
                        deficit_frac=deficit_frac.get(z, 0.0),
                        band=self._zone_moisture_band(z),
                        eto_factor=eto_factor,
                    )
                    st = self._hass.states.get(z) if self._hass else None
                    zone_names[z] = (st.attributes.get("friendly_name") if st else None) or z
            return build_daily_plan(planned, signals=signals, zone_names=zone_names, now=now)
        except Exception:  # advisory only — must never break health.json / the panel
            _LOGGER.exception("build_today_plan failed; returning empty plan")
            return DailyPlan(items=(), summary="Daily plan unavailable.")

    async def _refresh_auto_eto(self, now=None) -> None:
        """Recompute the auto ETo from the weather entity's daily forecast.

        No-op unless eto_auto is enabled. Any failure (no entity, service error,
        empty/unusable forecast, computed 0) leaves the previous value untouched
        so effective_eto() falls back to the manual figure — we never water on a
        guess. Wired to a daily 03:00 timer + an immediate call when toggled on."""
        if not self._config.get("eto_auto"):
            return
        # v1.49 — Open-Meteo provider: keyless, uses the HA lat/lon, and returns
        # FAO ET0 directly — no Home Assistant weather entity required.
        if str(self._config.get("eto_provider") or "ha") == "open_meteo":
            await self._refresh_auto_eto_open_meteo()
            return
        entity = self._resolve_weather_entity()
        if not entity:
            _LOGGER.debug("auto-ETo: no weather entity available")
            return
        state = self._hass.states.get(entity)
        if state is None:
            _LOGGER.debug("auto-ETo: weather entity %s has no state", entity)
            return
        try:
            resp = await self._hass.services.async_call(
                "weather",
                "get_forecasts",
                {"entity_id": entity, "type": "daily"},
                blocking=True,
                return_response=True,
            )
        except Exception as err:
            _LOGGER.warning("auto-ETo: forecast fetch failed for %s: %s", entity, err)
            return
        forecast = (resp or {}).get(entity, {}).get("forecast") or []
        if not forecast:
            _LOGGER.debug("auto-ETo: %s returned no daily forecast", entity)
            return

        from homeassistant.util import dt as dt_util

        now_local = dt_util.now()
        # Cache the near-term forecast daily HIGH (today + tomorrow) in F for the
        # rain-lockout ceiling. Done before the ETo compute so a rejected/garbage
        # ETo value doesn't also drop the heat signal.
        highs: list[float] = []
        for entry in forecast[:2]:
            fh = temp_to_fahrenheit(
                entry.get("temperature"), state.attributes.get("temperature_unit")
            )
            if fh is not None:
                highs.append(fh)
        if highs:
            self._forecast_high_f = max(highs)
            self._forecast_high_at = now_local
        try:
            eto = weekly_eto_inches_from_forecast(
                forecast,
                latitude_deg=float(self._hass.config.latitude),
                altitude_m=float(self._hass.config.elevation or 0.0),
                temp_unit=state.attributes.get("temperature_unit"),
                wind_unit=state.attributes.get("wind_speed_unit"),
                start_day_of_year=now_local.timetuple().tm_yday,
            )
        except Exception as err:
            _LOGGER.warning("auto-ETo: computation failed for %s: %s", entity, err)
            return
        # Reject anything outside the sane envelope the manual field enforces —
        # a flaky forecast (provider field-swap, unit glitch) must never become
        # the design basis. Keep the previous value / manual fallback instead.
        if not (0 < eto <= SANE_ETO_MAX):
            _LOGGER.warning(
                "auto-ETo: %.2f in/week from %s is out of sane range (0, %.0f] — keeping fallback",
                eto,
                entity,
                SANE_ETO_MAX,
            )
            return
        self._auto_eto_in_week = round(eto, 3)
        self._auto_eto_at = now_local
        _LOGGER.info(
            "auto-ETo: %.2f in/week from %s (%d-day forecast)",
            self._auto_eto_in_week,
            entity,
            len(forecast),
        )

    async def _refresh_auto_eto_open_meteo(self) -> None:
        """v1.49 — auto-ETo from Open-Meteo (keyless). Uses the HA lat/lon, reads
        FAO ET0 directly, and applies the SAME sane-range guard as the weather-
        entity path — a bad value leaves the previous / manual figure untouched."""
        import json as _json

        import aiohttp
        from homeassistant.helpers.aiohttp_client import async_get_clientsession
        from homeassistant.util import dt as dt_util

        from .open_meteo import open_meteo_url, parse_open_meteo

        lat = self._hass.config.latitude
        lon = self._hass.config.longitude
        if lat is None or lon is None:
            _LOGGER.debug("auto-ETo (Open-Meteo): no Home Assistant location set")
            return
        session = async_get_clientsession(self._hass)
        try:
            async with session.get(
                open_meteo_url(float(lat), float(lon), self._hass.config.time_zone),
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    _LOGGER.warning("auto-ETo (Open-Meteo): HTTP %s", resp.status)
                    return
                raw = await resp.content.read(200_000)
        except Exception as err:
            _LOGGER.warning("auto-ETo (Open-Meteo): fetch failed: %s", err)
            return
        try:
            data = parse_open_meteo(_json.loads(raw))
        except ValueError:
            _LOGGER.warning("auto-ETo (Open-Meteo): response was not valid JSON")
            return
        now_local = dt_util.now()
        if data["forecast_high_f"] is not None:
            self._forecast_high_f = data["forecast_high_f"]
            self._forecast_high_at = now_local
        eto = data["eto_week_in"]
        if eto is None or not (0 < eto <= SANE_ETO_MAX):
            _LOGGER.warning(
                "auto-ETo (Open-Meteo): %s in/week out of sane range (0, %.0f] — keeping fallback",
                eto,
                SANE_ETO_MAX,
            )
            return
        self._auto_eto_in_week = round(eto, 3)
        self._auto_eto_at = now_local
        _LOGGER.info(
            "auto-ETo: %.2f in/week from Open-Meteo (%d-day forecast)",
            self._auto_eto_in_week,
            data["days"],
        )

    # ── Restart fail-over (v1.30) ─────────────────────────────────

    def _resume_blocked_by_live_zone(self, zone_id: str, ghost_zones: set[str]) -> bool:
        """v1.39.1 — resume-time busy check: True when a DIFFERENT zone has a
        genuinely LIVE session — one that is NOT among this pass's own
        pre-restart ghosts (``ghost_zones``, whose timers died with HA and are
        not actually watering). On the initial resume pass every overlapping
        session is a ghost -> not busy (the one-per-pass flag serializes them
        instead); on a RETRY pass the snapshot is scoped to the deferred zones
        only, so a zone resumed earlier carries a fresh session OUTSIDE the
        ghost set and gates the retried zone for real, until it finishes (or
        the bounded retries give up and close the deferred zone out cleanly).
        Same committed-blocker definition as the resolver, independent-supply
        exemption included."""
        from homeassistant.util import dt as dt_util

        sessions = {
            z: s
            for z, s in (self._config.get("active_run_sessions") or {}).items()
            if z not in ghost_zones
        }
        if not sessions:
            return False
        blockers = committed_runs_from_sessions(
            sessions, dt_util.now(), independent_zones=self.independent_zones
        )
        return any(r.zone_entity_id != zone_id for r in blockers)

    async def _resume_interrupted_runs(self, _now=None, zones=None) -> None:
        """Resume (or close out) runs interrupted by an HA restart. For each
        persisted session: still within its planned window -> re-fire run_zone
        for the remaining minutes (re-chunks a giant); past its end -> drop it,
        force the valve off, and complete any 'zombie' running record. Bad/garbage
        session data fails safe to close (never resumes on a guess).

        `zones` (set) scopes a RETRY to only the zones that were deferred last time
        (switch not yet available), so a retry never re-fires zones already resumed.

        v1.39.1 — one-zone-at-a-time applies to RESUMES too. Gap insertion makes
        two overlapping shared-line sessions a normal system-created state (the
        inserted run's session sits inside the chunked owner's span by design);
        a restart during a live inserted run must NOT resume both concurrently.
        Only ONE shared-line session is dispatched per pass; the rest are
        deferred through the existing retry mechanism, where the first one's
        fresh (genuinely live) session then gates them until it finishes or the
        bounded retries give up and close them out cleanly (valve off + abort
        record) — the inviolable invariant beats completeness.
        """
        from homeassistant.core import callback
        from homeassistant.helpers.event import async_call_later
        from homeassistant.util import dt as dt_util

        sessions = dict(self._config.get("active_run_sessions") or {})
        if zones is None:
            self._resume_attempts = 0  # fresh resume sequence (also reset per HA start)
        else:
            sessions = {z: s for z, s in sessions.items() if z in zones}
        if not sessions:
            return
        entry_data = self._hass.data.get(DOMAIN, {}).get(self._entry_id)
        deferred: list[str] = []  # resumable zones whose switch isn't ready yet
        independent = self.independent_zones
        plan = plan_session_resume(sessions, dt_util.now())
        # The sessions THIS pass plans to resume are pre-restart ghosts (their
        # timers died with HA — nothing is actually watering), so they must not
        # count as "busy" when gating each other; serialization below is what
        # keeps them one-at-a-time. Any live session OUTSIDE this set (a zone
        # already resumed on an earlier pass, or a fresh run that started since)
        # is genuinely running and must gate a resume for real.
        ghost_zones = {
            it["zone"] for it in plan if it["action"] == "resume" and it["zone"] not in independent
        }
        shared_line_resumed = False
        for item in plan:
            zone = item["zone"]
            live = self._config.get("active_run_sessions") or {}
            # Honor any intervening change: if the session was cleared while we
            # awaited an earlier zone (e.g. the user pressed Stop), do NOT resume
            # it from our stale snapshot — that would resurrect a stopped run.
            if zone not in live:
                continue
            if item["action"] == "resume":
                # Gate on switch availability — a slow cloud reconnect (Rachio)
                # must NOT lose the run. If the switch isn't ready, keep the
                # session and retry rather than firing run_zone (which would
                # refuse) or dropping it.
                st = self._hass.states.get(zone)
                if st is None or st.state in ("unavailable", "unknown"):
                    # Mark the session gated so the UI doesn't show a false
                    # "Running" while the valve is actually off waiting to retry.
                    live[zone]["resuming_gated"] = True
                    deferred.append(zone)
                    continue
                # v1.39.1 — serialize shared-line resumes: one per pass, and
                # never into a controller genuinely busy elsewhere (see the
                # docstring + ghost_zones above). Deferral keeps the session
                # and retries — same treatment as an unavailable switch.
                if zone not in independent and (
                    shared_line_resumed or self._resume_blocked_by_live_zone(zone, ghost_zones)
                ):
                    live[zone]["resuming_gated"] = True
                    deferred.append(zone)
                    _LOGGER.info(
                        "Restart fail-over: deferring %s — the one-zone-at-a-time "
                        "controller is (or is about to be) busy on another zone",
                        zone,
                    )
                    continue
                remaining = item["remaining_minutes"]
                meta = item.get("session") or {}
                base_name = (meta.get("schedule_name") or "").strip()
                # Stash meta so the resumed run records under its schedule (marked
                # resumed), keeps its ORIGINAL source (manual stays manual), and
                # isn't mistaken for a fresh chunk block.
                if isinstance(entry_data, dict):
                    entry_data.setdefault("pending_run_meta", {})[zone] = {
                        "schedule_id": meta.get("schedule_id"),
                        "schedule_name": f"{base_name} (resumed)" if base_name else "Resumed",
                        "requested_minutes": remaining,
                        "source": meta.get("source"),
                        "triggers": {
                            "resumed": {"after_restart": True, "remaining_minutes": remaining}
                        },
                    }
                before_started = (live.get(zone) or {}).get("started_at")
                # Await so the resumed run records its fresh session before we save
                # config below (no stale-session-on-disk window).
                await self._hass.services.async_call(
                    DOMAIN, "run_zone", {"entity_id": zone, "minutes": remaining}, blocking=True
                )
                # Confirm the run actually started: run_zone re-records the session
                # with a fresh started_at on success. If it's unchanged (run_zone
                # refused, e.g. the switch dropped between the gate and now), the run
                # did NOT start -> re-defer for retry rather than leave a stale
                # session showing a false "Running".
                after = (self._config.get("active_run_sessions") or {}).get(zone)
                if after is None:
                    # Cleared externally (e.g. the user pressed Stop) during the
                    # resume — respect it; don't claim it resumed.
                    continue
                if after.get("started_at") == before_started:
                    # run_zone refused (switch dropped after the gate) — the run did
                    # NOT start; mark gated + re-defer rather than show false "Running".
                    after["resuming_gated"] = True
                    deferred.append(zone)
                    continue
                if zone not in independent:
                    # v1.39.1 — a shared-line valve is now genuinely open; every
                    # later shared-line session this pass defers to the retry.
                    shared_line_resumed = True
                _LOGGER.info(
                    "Restart fail-over: resuming %s for %d more min (to its planned end)",
                    zone,
                    remaining,
                )
            else:
                # Freshness guard: if a NEWER run took over this zone since our
                # snapshot (started_at changed — a fresh scheduled/manual run landed
                # while we awaited an earlier zone's resume), do NOT force it off. That
                # would kill a live, tracked run and start an off->on fight with the
                # external-off monitor. Let the new run own the zone.
                snap_started = (item.get("session") or {}).get("started_at")
                if (live.get(zone) or {}).get("started_at") != snap_started:
                    continue
                self._config.get("active_run_sessions", {}).pop(zone, None)
                await self._hass.services.async_call(
                    "switch", "turn_off", {"entity_id": zone}, blocking=False
                )
                self._run_history.complete_run(zone, ended_at=dt_util.now())
                self.note_zone_run_completed(zone)
                _LOGGER.info(
                    "Restart fail-over: %s was already past its planned end — "
                    "closed out + valve off",
                    zone,
                )
        await self.async_save_config()
        await self.async_save_run_history()

        # Switch not ready for some zones — keep their sessions and retry, up to
        # a bounded number of attempts, instead of losing the run. The retry is
        # SCOPED to just the deferred zones so it never re-fires zones already
        # resumed above (which would needlessly cancel + restart their timers).
        if deferred and self._resume_attempts < _RESUME_MAX_ATTEMPTS:
            self._resume_attempts += 1
            deferred_set = set(deferred)
            _LOGGER.info(
                "Restart fail-over: %s switch(es) not ready, retry %d/%d in %ds",
                len(deferred),
                self._resume_attempts,
                _RESUME_MAX_ATTEMPTS,
                _RESUME_RETRY_SECONDS,
            )

            # @callback so the retry runs on the event loop, not an executor
            # thread (where async_create_task raises and the retry is lost).
            @callback
            def _fire_retry(_now):
                self._hass.async_create_task(self._resume_interrupted_runs(zones=deferred_set))

            self._cancel_resume = async_call_later(self._hass, _RESUME_RETRY_SECONDS, _fire_retry)
        elif deferred:
            # Out of retries — DON'T leave a zombie session (it would show a false
            # "Running" on the card for hours). Drop it, force the valve off as a
            # safety, and record the run as aborted.
            _LOGGER.warning(
                "Restart fail-over: gave up resuming %s after %d attempts — switch "
                "still unavailable; closing out + valve off",
                deferred,
                _RESUME_MAX_ATTEMPTS,
            )
            for zone in deferred:
                self._config.get("active_run_sessions", {}).pop(zone, None)
                await self._hass.services.async_call(
                    "switch", "turn_off", {"entity_id": zone}, blocking=False
                )
                self._run_history.abort_run(
                    zone,
                    ended_at=dt_util.now(),
                    reason="restart resume gave up — switch unavailable",
                )
            await self.async_save_config()
            await self.async_save_run_history()
