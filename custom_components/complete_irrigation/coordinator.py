"""Schedule coordinator — loads persisted schedules + fires due runs on a tick.

Each config entry gets its own `ScheduleCoordinator`. On setup:
  1. Load the ScheduleStore from HA's storage helper
  2. Subscribe to a 60-second tick
  3. On each tick: compute runs in (last_tick, now] using the pure-logic
     `due_runs_since`, fire each one via the existing `run_zone` service

On unload: cancel the tick subscription.

After any service-driven schedule change (add / update / delete / enable),
the service handler calls `coordinator.async_save()` to persist.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from .const import DOMAIN
from .run_planner import due_runs_since, next_runs
from .schedule import ScheduleStore

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Bump if the schedule storage shape ever changes incompatibly.
STORAGE_VERSION = 1
TICK_SECONDS = 60


def _storage_key(entry_id: str) -> str:
    return f"{DOMAIN}.schedules.{entry_id}"


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


class ScheduleCoordinator:
    """Per-entry orchestrator for persisted schedules + per-tick firing."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._store: ScheduleStore = ScheduleStore()
        self._last_tick: datetime | None = None
        self._cancel_tick = None

    @property
    def schedule_store(self) -> ScheduleStore:
        return self._store

    async def async_setup(self) -> None:
        """Load persisted schedules and start the tick loop."""
        from homeassistant.helpers.event import async_track_time_interval
        from homeassistant.util import dt as dt_util

        self._store = await _async_load_schedules(self._hass, self._entry_id)
        self._last_tick = dt_util.now()
        self._cancel_tick = async_track_time_interval(
            self._hass, self._tick, timedelta(seconds=TICK_SECONDS)
        )
        _LOGGER.info(
            "Schedule coordinator started for entry %s — %d schedule(s) loaded",
            self._entry_id,
            len(self._store.all()),
        )

    def async_unload(self) -> None:
        """Stop the tick loop. Called when the config entry unloads."""
        if self._cancel_tick:
            self._cancel_tick()
            self._cancel_tick = None
        _LOGGER.info("Schedule coordinator stopped for entry %s", self._entry_id)

    async def async_save(self) -> None:
        """Persist the current schedule store to HA storage."""
        await _async_save_schedules(self._hass, self._entry_id, self._store)

    async def _tick(self, now: datetime) -> None:
        """One pass: find runs due since last_tick, fire each one."""
        if self._last_tick is None:
            self._last_tick = now
            return

        last = self._last_tick
        self._last_tick = now

        # Window from last tick to (now + 60s) so we don't miss runs that
        # fall a few seconds outside the tick interval. due_runs_since
        # then keeps only those <= now.
        upcoming = next_runs(
            self._store.enabled_schedules(),
            from_dt=last,
            until_dt=now + timedelta(seconds=TICK_SECONDS),
        )
        due = due_runs_since(upcoming, last, now)

        for run in due:
            _LOGGER.info(
                "Firing scheduled run: %s for %d min (schedule %s)",
                run.zone_entity_id,
                run.duration_minutes,
                run.schedule_name,
            )
            try:
                await self._hass.services.async_call(
                    DOMAIN,
                    "run_zone",
                    {
                        "entity_id": run.zone_entity_id,
                        "minutes": run.duration_minutes,
                    },
                    blocking=False,
                )
            except Exception:
                _LOGGER.exception("Failed to fire scheduled run for %s", run.zone_entity_id)
