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

from .conflict_resolver import POLICY_DEFER_NEW, resolve_conflicts
from .const import DOMAIN
from .moisture_gate import COMBINE_AVERAGE, combine_moisture, evaluate_moisture
from .notifications import (
    CATEGORY_IMPORTANT,
    NotificationDispatcher,
)
from .run_planner import due_runs_since, next_runs
from .schedule import ScheduleStore
from .weather_gate import evaluate_hot_weather, evaluate_rain_lockout

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
TICK_SECONDS = 60


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

    @property
    def schedule_store(self) -> ScheduleStore:
        return self._store

    @property
    def config(self) -> dict[str, Any]:
        return self._config

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

        # Notifications
        self.notifier = NotificationDispatcher(self._hass)
        notif_cfg = self._config.get("notifications", {})
        self.notifier.update_config(**notif_cfg)

        self._last_tick = dt_util.now()
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

        _LOGGER.info(
            "Schedule coordinator started for entry %s — %d schedule(s) loaded",
            self._entry_id,
            len(self._store.all()),
        )

    def async_unload(self) -> None:
        for cancel_attr in ("_cancel_tick", "_cancel_summary", "_cancel_weekly"):
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

    async def _fire_low_moisture_summary(self) -> None:
        """Walk zones; notify if any bound moisture sensor is below min%."""
        if not self.notifier:
            return
        zones = self._config.get("zones", {})
        offenders: list[str] = []
        for zone_id, zone_cfg in zones.items():
            sensors = zone_cfg.get("moisture_entities") or []
            min_pct = zone_cfg.get("min_pct")
            if not sensors or min_pct is None:
                continue
            for eid in sensors:
                state = self._hass.states.get(eid)
                if state is None:
                    continue
                if state.state in ("unknown", "unavailable"):
                    continue
                try:
                    val = float(state.state)
                except (TypeError, ValueError):
                    continue
                if val < float(min_pct):
                    friendly = state.attributes.get("friendly_name") or eid
                    offenders.append(f"• {zone_id}: {friendly} = {val:.1f}% (min {min_pct}%)")
                    break  # one alert per zone is enough
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
        from homeassistant.util import dt as dt_util

        n = now or dt_util.now()
        if n.weekday() != 6:  # Sunday only
            return
        if not self.notifier:
            return
        zone_count = len(self._config.get("zones", {}))
        sched_count = len(self._store.all())
        msg = (
            f"Weekly check: {sched_count} schedule(s) across {zone_count} configured "
            f"zone(s). Confirm everything looks right in the Irrigation panel."
        )
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

    def clear_lockout(self) -> None:
        if self.lockout_until is not None:
            self.lockout_until = None
            self._notify_lockout_change()

    # ── Tick logic ─────────────────────────────────────────────────

    async def _tick(self, now: datetime) -> None:
        if self._last_tick is None:
            self._last_tick = now
            return

        # Evaluate rain lockout on every tick.
        await self._evaluate_lockout(now)

        last = self._last_tick
        self._last_tick = now

        # If currently locked out, no scheduled runs fire (we still
        # advance last_tick so we don't backfire a flood when lockout
        # expires).
        if self.is_locked_out_now():
            return

        upcoming = next_runs(
            self._store.enabled_schedules(),
            from_dt=last - timedelta(hours=2),
            until_dt=now + timedelta(hours=2),
        )
        resolved = resolve_conflicts(upcoming, POLICY_DEFER_NEW)
        due = due_runs_since(resolved, last, now)

        for run in due:
            if run.reason.startswith("skipped:"):
                _LOGGER.info(
                    "Skipping run for %s (%s): %s",
                    run.zone_entity_id,
                    run.schedule_name,
                    run.reason,
                )
                continue
            await self._fire_run(run)

    async def _fire_run(self, run) -> None:
        """Evaluate moisture + hot-weather modifiers for `run`, then fire it."""
        zone_id = run.zone_entity_id
        zone_cfg = self._config["zones"].get(zone_id, {})
        adjusted_minutes = run.duration_minutes
        skip_reasons: list[str] = []
        boost_reasons: list[str] = []

        # ── Moisture (if sensor configured for this zone) ──
        moisture_entities = zone_cfg.get("moisture_entities") or []
        if moisture_entities:
            readings: list[float] = []
            for ent in moisture_entities:
                state = self._hass.states.get(ent)
                if state is None or state.state in ("unknown", "unavailable"):
                    continue
                try:
                    readings.append(float(state.state))
                except (TypeError, ValueError):
                    continue
            combine_mode = zone_cfg.get("combine_mode", COMBINE_AVERAGE)
            current = combine_moisture(readings, combine_mode)
            if current is not None:
                decision = evaluate_moisture(
                    current=current,
                    min_pct=float(zone_cfg.get("min_pct", 21)),
                    target=float(zone_cfg.get("target_pct", 31)),
                    max_pct=float(zone_cfg.get("max_pct", 40)),
                    base_minutes=adjusted_minutes,
                )
                _LOGGER.info("Moisture decision for %s: %s", zone_id, decision.reason)
                if decision.skip:
                    skip_reasons.append(decision.reason)
                else:
                    adjusted_minutes = decision.runtime_minutes

        # ── Hot weather boost (single, integration-wide for now) ──
        if not skip_reasons:
            forecast_high = self._read_forecast_high()
            threshold = self._config.get("hot_threshold_f")
            boost_pct = self._config.get("boost_percent", 0)
            if forecast_high is not None and threshold is not None:
                hw = evaluate_hot_weather(
                    daily_high_f=forecast_high,
                    threshold_f=float(threshold),
                    boost_percent=int(boost_pct),
                )
                if hw.boost:
                    adjusted_minutes = max(1, round(adjusted_minutes * hw.multiplier))
                    boost_reasons.append(
                        f"hot-weather boost (high {forecast_high}F >= {threshold}F)"
                        f" x{hw.multiplier:.2f}"
                    )

        if skip_reasons:
            _LOGGER.info(
                "Skipped scheduled run for %s: %s",
                zone_id,
                "; ".join(skip_reasons),
            )
            return

        for r in boost_reasons:
            _LOGGER.info("Adjusted run for %s: %s", zone_id, r)

        _LOGGER.info(
            "Firing run: %s for %d min (schedule %s)",
            zone_id,
            adjusted_minutes,
            run.schedule_name,
        )
        try:
            await self._hass.services.async_call(
                DOMAIN,
                "run_zone",
                {"entity_id": zone_id, "minutes": adjusted_minutes},
                blocking=False,
            )
        except Exception:
            _LOGGER.exception("Failed to fire scheduled run for %s", zone_id)

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
