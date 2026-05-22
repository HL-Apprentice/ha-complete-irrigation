"""HA Calendar platform for upcoming irrigation runs.

Single entity `calendar.complete_irrigation` that materializes the next
N days of scheduled runs from the per-entry ScheduleCoordinator's
ScheduleStore, using the pure-logic `RunPlanner.next_runs`.

v1.1 — Slice 15: `async_create_event` allows adding a one-off run from
HA's Calendar dashboard. Delete/update are deferred to v1.2.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.components.calendar import CalendarEntity, CalendarEntityFeature, CalendarEvent
from homeassistant.util import dt as dt_util

from .conflict_resolver import POLICY_DEFER_NEW, resolve_conflicts
from .const import DOMAIN
from .run_planner import next_runs
from .schedule import Schedule

_LOGGER = logging.getLogger(__name__)

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
    _attr_supported_features = CalendarEntityFeature.CREATE_EVENT

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

    async def async_create_event(self, **kwargs) -> None:
        """Add a one-off run from HA's Calendar dashboard.

        Maps the create-event call to a bounded Schedule:
          - weekdays = just the start day's weekday
          - end_date = start day (so the schedule fires exactly once)
          - duration = end - start in minutes

        Summary should follow "Zone Name" — but we accept any text and
        store it as the schedule name.
        """
        from homeassistant.const import EVENT_CALL_SERVICE  # noqa: F401  (touches HA event loop)

        dtstart = kwargs.get("dtstart")
        dtend = kwargs.get("dtend")
        summary = (kwargs.get("summary") or "Calendar entry").strip()

        if not dtstart or not dtend:
            raise ValueError("dtstart and dtend are required")

        # If user enters "Front Lawn 15 min" we still need a zone entity.
        # For v1.1 we require the summary to match an existing zone's
        # friendly name (case-insensitive). If we can't resolve, raise.
        zone_id = self._resolve_zone_from_summary(summary)
        if zone_id is None:
            raise ValueError(
                "Couldn't resolve a configured zone from event summary. "
                "Use a zone's friendly name (e.g., 'Front Lawn') as the event title."
            )

        duration_min = int((dtend - dtstart).total_seconds() // 60)
        if duration_min <= 0:
            raise ValueError("Event duration must be positive")

        sched = Schedule(
            id=uuid.uuid4().hex[:12],
            name=f"Calendar: {summary}",
            zone_entity_id=zone_id,
            start_time=dtstart.time(),
            duration_minutes=duration_min,
            weekdays=(dtstart.weekday(),),
            enabled=True,
            end_date=dtstart.date(),
            created_via="calendar",
        )
        self._coordinator.schedule_store.add(sched)
        await self._coordinator.async_save()
        _LOGGER.info(
            "Created one-off run from calendar: %s @ %s for %d min",
            zone_id,
            dtstart.isoformat(),
            duration_min,
        )

    def _resolve_zone_from_summary(self, summary: str) -> str | None:
        """Find a configured zone whose friendly_name matches the event summary."""
        # Available zones come from the entry data (set on setup).
        target = summary.strip().lower()
        # Look across all entries via hass to find the zone list.
        for _key, data in (self.hass.data.get(DOMAIN, {}) or {}).items():
            if not isinstance(data, dict):
                continue
            zones = data.get("zones") or []
            for ent_id in zones:
                state = self.hass.states.get(ent_id)
                friendly = (
                    state.attributes.get("friendly_name") if state and state.attributes else None
                ) or ent_id.split(".", 1)[-1].replace("_", " ")
                if friendly.lower() == target or ent_id.lower() == target:
                    return ent_id
        return None


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
