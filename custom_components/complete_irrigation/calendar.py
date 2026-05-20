"""HA Calendar platform for upcoming irrigation runs.

Single entity `calendar.complete_irrigation` that materializes the next
N days of scheduled runs from the per-entry ScheduleCoordinator's
ScheduleStore, using the pure-logic `RunPlanner.next_runs`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.util import dt as dt_util

from .conflict_resolver import POLICY_DEFER_NEW, resolve_conflicts
from .const import DOMAIN
from .run_planner import next_runs

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the irrigation calendar entity."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator = entry_data["coordinator"]
    async_add_entities([IrrigationCalendar(coordinator, entry.entry_id)])


class IrrigationCalendar(CalendarEntity):
    """Calendar showing planned irrigation runs from all enabled schedules."""

    _attr_has_entity_name = True
    _attr_name = "Irrigation"
    _attr_icon = "mdi:sprinkler-variant"

    def __init__(self, coordinator, entry_id: str) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_calendar"

    @property
    def event(self) -> CalendarEvent | None:
        """Return the next upcoming event (or one currently running)."""
        now = dt_util.now()
        # Look ahead 30 days for the next event.
        raw = next_runs(
            self._coordinator.schedule_store.enabled_schedules(),
            from_dt=now - timedelta(hours=1),  # include events that started recently
            until_dt=now + timedelta(days=30),
        )
        upcoming = [
            r
            for r in resolve_conflicts(raw, POLICY_DEFER_NEW)
            if not r.reason.startswith("skipped:")
        ]
        for run in upcoming:
            end = run.start_at + timedelta(minutes=run.duration_minutes)
            if end >= now:  # not yet finished
                return _to_event(run)
        return None

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        """Return all scheduled runs in [start_date, end_date)."""
        raw = next_runs(
            self._coordinator.schedule_store.enabled_schedules(),
            from_dt=start_date,
            until_dt=end_date,
        )
        runs = [
            r
            for r in resolve_conflicts(raw, POLICY_DEFER_NEW)
            if not r.reason.startswith("skipped:")
        ]
        return [_to_event(r) for r in runs]


def _to_event(run) -> CalendarEvent:
    """Convert a PlannedRun to an HA CalendarEvent."""
    return CalendarEvent(
        start=run.start_at,
        end=run.start_at + timedelta(minutes=run.duration_minutes),
        summary=f"{run.schedule_name} — {run.zone_entity_id.split('.', 1)[-1]}",
        description=(
            f"Schedule: {run.schedule_name}\n"
            f"Zone: {run.zone_entity_id}\n"
            f"Duration: {run.duration_minutes} min\n"
            f"Reason: {run.reason}"
        ),
        uid=f"{run.schedule_id}_{run.start_at.isoformat()}",
    )
