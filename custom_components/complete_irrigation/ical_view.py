"""iCal feed view — subscribe with any external calendar app.

Serves an ICS document at /api/complete_irrigation/calendar.ics.
Read-only. Includes the next 30 days of planned runs (resolved through
the conflict resolver) from the first config entry's coordinator.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.components.http import HomeAssistantView

from .conflict_resolver import POLICY_DEFER_NEW, resolve_conflicts
from .const import DOMAIN
from .run_planner import next_runs

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOOKAHEAD_DAYS = 30


def _ics_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")


def _ics_dt(dt) -> str:
    """Format datetime as iCal UTC stamp: YYYYMMDDTHHMMSSZ."""
    from homeassistant.util import dt as dt_util

    utc = dt_util.as_utc(dt)
    return utc.strftime("%Y%m%dT%H%M%SZ")


class IrrigationCalendarICSView(HomeAssistantView):
    """Read-only ICS feed for the integration's upcoming runs."""

    url = f"/api/{DOMAIN}/calendar.ics"
    name = f"{DOMAIN}:calendar_ics"
    requires_auth = False  # public read-only — schedule data isn't sensitive

    async def get(self, request):
        from aiohttp import web
        from homeassistant.util import dt as dt_util

        from . import _SHARED_KEY

        hass: HomeAssistant = request.app["hass"]
        coord = None
        for key, data in hass.data.get(DOMAIN, {}).items():
            if key == _SHARED_KEY:
                continue
            coord = data.get("coordinator")
            if coord is not None:
                break

        if coord is None:
            return web.Response(text="No coordinator configured", status=503)

        now = dt_util.now()
        raw = next_runs(
            coord.schedule_store.enabled_schedules(),
            from_dt=now,
            until_dt=now + timedelta(days=_LOOKAHEAD_DAYS),
        )
        runs = [
            r
            for r in resolve_conflicts(raw, POLICY_DEFER_NEW)
            if not r.reason.startswith("skipped:")
        ]

        lines = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//HL-Apprentice//complete_irrigation//EN",
            "CALSCALE:GREGORIAN",
            "METHOD:PUBLISH",
            "X-WR-CALNAME:Irrigation",
        ]
        for r in runs:
            start = r.start_at
            end = start + timedelta(minutes=r.duration_minutes)
            zone_label = r.zone_entity_id.split(".", 1)[-1]
            description = (
                f"Zone: {r.zone_entity_id} | "
                f"Duration: {r.duration_minutes} min | "
                f"Reason: {r.reason}"
            )
            lines.extend(
                [
                    "BEGIN:VEVENT",
                    f"UID:{r.schedule_id}_{start.isoformat()}@complete_irrigation",
                    f"DTSTAMP:{_ics_dt(now)}",
                    f"DTSTART:{_ics_dt(start)}",
                    f"DTEND:{_ics_dt(end)}",
                    f"SUMMARY:{_ics_escape(f'{r.schedule_name} - {zone_label}')}",
                    f"DESCRIPTION:{_ics_escape(description)}",
                    "END:VEVENT",
                ]
            )
        lines.append("END:VCALENDAR")

        body = "\r\n".join(lines) + "\r\n"
        return web.Response(
            body=body,
            content_type="text/calendar",
            charset="utf-8",
            headers={"Content-Disposition": 'inline; filename="irrigation.ics"'},
        )


def async_register_ical_view(hass: HomeAssistant) -> None:
    """Register the ICS feed view. Idempotent."""
    if hass.http is None:
        return
    hass.http.register_view(IrrigationCalendarICSView())
