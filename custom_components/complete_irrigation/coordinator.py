"""Schedule coordinator — loads persisted schedules + fires due runs on a tick.

Per-entry orchestrator. Each tick:
  1. Compute planned runs (next_runs)
  2. Resolve conflicts (resolve_conflicts, default policy = DEFER_NEW)
  3. Check rain lockout (skip all if active)
  4. For each due run:
       a. Read per-zone moisture sensors (if configured) → MoistureGate
       b. Read forecast → hot-weather boost multiplier
       c. Fire via complete_irrigation.run_zone with adjusted duration,
          or skip with logged reason
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from .conflict_resolver import (
    DEFAULT_CASCADE_CAP_MINUTES,
    POLICY_DEFER_NEW,
    POLICY_SHIFT_EXISTING_EARLIER,
    POLICY_SPLIT_DIFFERENCE,
    REASON_DEFERRED_LATE,
    resolve_conflicts,
)
from .const import (
    DEFAULT_BUFFER_MINUTES,
    DEFAULT_ZONE_MAX_PCT,
    DEFAULT_ZONE_MIN_PCT,
    DEFAULT_ZONE_TARGET_PCT,
    DOMAIN,
)

# v1.16 — pure-logic helpers live in coordinator_logic.py now; re-exported
# here so every existing `from .coordinator import build_weekly_zone_summary`
# (and friends) keeps resolving.
from .coordinator_logic import (
    build_weekly_zone_summary,
    compute_expired_establishment_schedules,
    compute_low_moisture_offenders,
    detect_sensor_offline_transitions,
)
from .moisture_gate import COMBINE_AVERAGE, combine_moisture, evaluate_moisture
from .notifications import (
    CATEGORY_IMPORTANT,
    NotificationDispatcher,
)
from .plant import PlantStore
from .run_history import (
    DEFAULT_RETENTION_DAYS,
    RunHistoryStore,
)
from .run_planner import due_runs_since, longest_run_span_minutes, next_runs
from .schedule import ScheduleStore
from .weather_gate import evaluate_hot_weather, evaluate_rain_lockout, evaluate_wind_defer

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

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


async def _async_save_plants(hass: HomeAssistant, entry_id: str, store: PlantStore) -> None:
    from homeassistant.helpers.storage import Store

    helper = Store(hass, STORAGE_VERSION, _storage_key(entry_id, "plants"))
    await helper.async_save({"plants": store.to_serializable()})


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
        # Lockout state
        self.lockout_until: datetime | None = None
        self._lockout_listeners: list[Callable[[], None]] = []
        # Notification dispatcher (initialized in async_setup)
        self.notifier: NotificationDispatcher | None = None
        self._cancel_summary = None
        self._cancel_weekly = None
        self._cancel_post_install = None
        # Sensor-availability tracking (PRD #57): remembers which bound
        # moisture sensors are currently offline so we only push a
        # notification once per crossing (not on every tick).
        self._offline_sensors: set[str] = set()
        # End-of-establishment tracking (PRD #76): schedule ids we've
        # already prompted about, so the prompt doesn't fire daily.
        self._establishment_prompted: set[str] = set()
        # Run history (v1.14): every completed / skipped / aborted run.
        # Loaded in async_setup, pruned at load + on each tick boundary.
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

    @property
    def schedule_store(self) -> ScheduleStore:
        return self._store

    @property
    def config(self) -> dict[str, Any]:
        return self._config

    @property
    def run_history(self) -> RunHistoryStore:
        return self._run_history

    @property
    def plants(self) -> PlantStore:
        return self._plants

    def register_lockout_listener(self, callback: Callable[[], None]) -> None:
        self._lockout_listeners.append(callback)

    def _notify_lockout_change(self) -> None:
        for cb in self._lockout_listeners:
            try:
                cb()
            except Exception:
                _LOGGER.exception("Lockout listener errored")

    def is_locked_out_now(self) -> bool:
        if self.lockout_until is None:
            return False
        from homeassistant.util import dt as dt_util

        return dt_util.now() < self.lockout_until

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

        # Morning summary — fires daily at the end of quiet hours.
        eh, em = self.notifier.quiet_hours_end.split(":")
        self._cancel_summary = async_track_time_change(
            self._hass, self._fire_morning_summary, hour=int(eh), minute=int(em), second=0
        )

        # Weekly verification reminder — Sunday 08:00 default
        self._cancel_weekly = async_track_time_change(
            self._hass, self._fire_weekly_reminder, hour=8, minute=0, second=0
        )

        # PRD #78 — one-shot 7-day post-install reminder. Tracked in
        # coordinator config so it only fires once per integration setup,
        # not on every HA restart.
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
                self._cancel_post_install = async_call_later(
                    self._hass,
                    delay_seconds,
                    lambda _now: self._hass.async_create_task(self._fire_post_install_reminder()),
                )

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
            "_cancel_post_install",
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

    def clear_lockout(self) -> None:
        if self.lockout_until is not None:
            self.lockout_until = None
            self._notify_lockout_change()

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

        # v1.17.3 — if locked out, only schedules with
        # ignore_rain_lockout=True still fire. Others wait for the
        # lockout to expire. last_tick still advances either way so
        # we don't backfire a flood when lockout clears.
        locked_out = self.is_locked_out_now()

        # PRD #38 — honor a user-configured inter-zone valve buffer.
        zone_buffer = self._config.get("zone_buffer_seconds")
        enabled_scheds = self._store.enabled_schedules()
        if locked_out:
            enabled_scheds = [s for s in enabled_scheds if s.ignore_rain_lockout]
            if not enabled_scheds:
                return
            _LOGGER.info(
                "Rain lockout active; %d schedule(s) with ignore_rain_lockout=True still firing",
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
        )
        # Honor a user-selected global conflict policy (Settings tab).
        # Unknown / missing → keep safe default of POLICY_DEFER_NEW.
        configured = self._config.get("conflict_policy", POLICY_DEFER_NEW)
        policy = configured if configured in _VALID_CONFLICT_POLICIES else POLICY_DEFER_NEW
        # v1.20 — NEVER-DROP. A run that can't be placed within the cascade
        # cap is deferred *late* and still fires, instead of being skipped.
        # Compression (shrinking the long runs to keep a squeezed run on
        # time) is intentionally OFF here for now: shrinking a run that has
        # already started would mis-model the controller and could fire a
        # zone into a still-running one. It lands in v1.21 alongside
        # in-progress-run tracking (the resolver + sim already support it,
        # gated by compress_floor_pct < 100 + REASON_COMMITTED blockers).
        resolved = resolve_conflicts(
            upcoming,
            policy,
            compress_floor_pct=100,  # 100 = no compression (defer-late only)
        )
        due = due_runs_since(resolved, last, now)

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
            await self._fire_run(run)

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
            "Firing run: %s for %d min (schedule %s)",
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

    # ── Auto-soak recovery (v1.19) ────────────────────────────────

    async def _tick_auto_soak(self, now: datetime) -> None:
        """Closed-loop low-moisture recovery, driven once per tick.

        For each zone with auto_soak_enabled: when effective moisture
        (excluded sensors filtered out) drops below min_pct, run the
        zone for soak_run_minutes, wait soak_wait_minutes for the water
        to absorb, re-read, and repeat — until moisture >= min_pct or
        soak_max_cycles is exhausted (then notify + cooldown).
        """
        if self.is_locked_out_now():
            # Rain lockout: abandon any in-flight sequences — rain is
            # doing the job — and don't start new ones.
            if self._soak_state:
                _LOGGER.info(
                    "Auto-soak: rain lockout active, abandoning %d sequence(s)",
                    len(self._soak_state),
                )
                self._soak_state.clear()
            return

        for zone_id, zone_cfg in (self._config.get("zones") or {}).items():
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
        new_minutes = max(1, round(adjusted_minutes * hw.multiplier))
        note = f"hot-weather boost (high {forecast_high}F >= {threshold}F) x{hw.multiplier:.2f}"
        return new_minutes, note

    # ── Rain lockout ──────────────────────────────────────────────

    async def _evaluate_lockout(self, now: datetime) -> None:
        """Read rain sensor; activate / extend lockout if rain seen."""
        rain_entity = self._config.get("rain_sensor")
        if not rain_entity:
            return

        state = self._hass.states.get(rain_entity)
        if state is None:
            return
        try:
            rainfall_in = float(state.state)
        except (TypeError, ValueError):
            return

        hours = evaluate_rain_lockout(rainfall_in)
        if hours is None:
            return

        new_until = now + timedelta(hours=hours)
        # Only extend lockout, never shorten it within the same rain event.
        if self.lockout_until is None or new_until > self.lockout_until:
            became_active = self.lockout_until is None
            self.lockout_until = new_until
            _LOGGER.info(
                "Rain lockout active — %sin rain triggered %dh lockout (until %s)",
                rainfall_in,
                hours,
                new_until.isoformat(),
            )
            self._notify_lockout_change()
            if became_active and self.notifier:
                await self.notifier.notify(
                    f"Rain detected ({rainfall_in:.2f} in) — all watering paused for {hours}h",
                    title="Rain lockout active",
                    category=CATEGORY_IMPORTANT,
                    event_type="rain_lockout_started",
                    extra={"until": new_until.isoformat(), "hours": hours},
                )

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
