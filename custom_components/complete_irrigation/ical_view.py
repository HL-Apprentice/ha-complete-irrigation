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
    """Escape per RFC 5545 §3.3.11. Strips control characters (other than
    the escaped newline) so user-controlled strings like schedule_name
    cannot inject CRLF + new VCALENDAR fields into the feed."""
    # Drop control chars first — \r, \t, NUL, etc. Keep \n; we encode it next.
    cleaned = "".join(c for c in s if c == "\n" or c >= " ")
    return (
        cleaned.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")
    )


def _ics_dt(dt) -> str:
    """Format datetime as iCal UTC stamp: YYYYMMDDTHHMMSSZ."""
    from homeassistant.util import dt as dt_util

    utc = dt_util.as_utc(dt)
    return utc.strftime("%Y%m%dT%H%M%SZ")


class IrrigationCalendarICSView(HomeAssistantView):
    """Read-only ICS feed for the integration's upcoming runs."""

    url = f"/api/{DOMAIN}/calendar.ics"
    name = f"{DOMAIN}:calendar_ics"
    # Auth required (v1.15). Schedule names + entity_ids would otherwise be
    # readable by anyone who can reach the HA HTTP port — including the
    # public Nabu Casa URL — and they leak occupancy patterns (when
    # irrigation runs ⇒ house is in landscaping mode). Calendar subscribers
    # can pass an HA long-lived access token via the ?authSig= query param
    # or via cookie when subscribed from inside the network.
    requires_auth = True

    async def get(self, request):
        from aiohttp import web
        from homeassistant.util import dt as dt_util

        from . import find_coordinator

        hass: HomeAssistant = request.app["hass"]
        # Admin-only (parity with the health feed + WS commands): schedule names +
        # entity_ids + run times leak occupancy patterns. Calendar subscribers must
        # use an ADMIN long-lived token.
        user = request.get("hass_user")
        if user is None or not getattr(user, "is_admin", False):
            from homeassistant.exceptions import Unauthorized

            raise Unauthorized()
        coord = find_coordinator(hass)

        if coord is None:
            return web.Response(text="No coordinator configured", status=503)

        now = dt_util.now()
        raw = next_runs(
            coord.schedule_store.enabled_schedules(),
            from_dt=now,
            until_dt=now + timedelta(days=_LOOKAHEAD_DAYS),
            sun_times=coord.sun_times,
        )
        runs = [
            r
            for r in resolve_conflicts(
                raw, POLICY_DEFER_NEW, independent_zones=coord.independent_zones
            )
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
                    # schedule_id is escaped too: Schedule.from_dict takes id
                    # straight from .storage without validation, so a corrupted
                    # store must not be able to inject CRLF + new ICS fields.
                    f"UID:{_ics_escape(r.schedule_id)}_{start.isoformat()}@complete_irrigation",
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
