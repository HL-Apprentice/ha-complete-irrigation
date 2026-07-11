"""Health JSON view — machine-readable irrigation status for the LLM monitor.

Serves a read-only JSON document at /api/complete_irrigation/health.json with
everything the watchdog needs to enforce the prime directive ("every plant gets
its water, every time; detect a miss within one cycle"):

  • the schedule + cadence and each schedule's next planned run,
  • which plants are on each loop and their drips / volume (the WUCOLS x ETo
    water-need design),
  • the recent run outcomes + what's running now,
  • a PRE-COMPUTED list of misses (aborted / skipped / short runs in the last
    24 h) so a consumer has a deterministic alarm signal even when the LLM is
    unavailable — the model then adds judgment (cadence-overdue, under-watered)
    on top.

Read-only. requires_auth=True (same as the iCal feed): schedule / plant /
occupancy data leaks landscaping patterns, so it must not be reachable
unauthenticated. The monitor authenticates with a long-lived access token.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.components.http import HomeAssistantView

from .const import DOMAIN
from .daily_planner import serialize_daily_plan
from .hydraulics import DEFAULT_EFFICIENCY
from .plant_design import build_yard_report, serialize_loop_report
from .run_history import STATUS_ABORTED, STATUS_COMPLETED, STATUS_SKIPPED
from .run_planner import next_runs

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

_SCHEMA_VERSION = 1
_NEXT_RUN_LOOKAHEAD_DAYS = 7
_RECENT_RUNS = 40
_MISS_WINDOW_HOURS = 24
_SHORT_RUN_FRACTION = 0.8  # completed but < 80% of requested = cut short

_WEEKDAY_ABBR = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _cadence_summary(sched) -> str:
    """Human-readable firing cadence for a schedule (for the LLM to reason about
    'overdue')."""
    mode = getattr(sched, "mode", "weekdays")
    if mode == "interval_hours" and sched.interval_hours:
        return f"every {sched.interval_hours}h"
    if mode == "interval" and sched.interval_days:
        return f"every {sched.interval_days}d"
    days = getattr(sched, "weekdays", ()) or ()
    valid = [d for d in sorted(days) if 0 <= d <= 6]
    if valid:
        return "weekly: " + ",".join(_WEEKDAY_ABBR[d] for d in valid)
    return mode


def _detect_misses(history, now):
    """Pure scan of recent records for aborted / skipped / short runs in the last
    `_MISS_WINDOW_HOURS`. Returns (issues, aborted, skipped, short).

    This is the deterministic floor of the monitor: a consumer can alarm on these
    without ever calling the LLM (defense-in-depth for "this miss can't happen")."""
    from homeassistant.util import dt as dt_util

    cutoff = now - timedelta(hours=_MISS_WINDOW_HOURS)
    issues: list[str] = []
    aborted = skipped = short = 0
    for rec in history:
        started = dt_util.parse_datetime(rec.started_at) if rec.started_at else None
        if started is None or started < cutoff:
            continue
        when = dt_util.as_local(started).strftime("%a %H:%M")
        name = rec.zone_name or rec.zone_entity_id
        req = int(rec.requested_minutes or 0)
        act = int(rec.actual_minutes or 0)
        if rec.status == STATUS_ABORTED:
            aborted += 1
            issues.append(f"{name} ABORTED at {act}/{req} min ({rec.reason or 'unknown'}) - {when}")
        elif rec.status == STATUS_SKIPPED:
            skipped += 1
            issues.append(f"{name} SKIPPED ({rec.reason or 'unknown'}) - {when}")
        elif rec.status == STATUS_COMPLETED and req > 0 and act < req * _SHORT_RUN_FRACTION:
            short += 1
            issues.append(f"{name} SHORT {act}/{req} min - {when}")
    return issues, aborted, skipped, short


class IrrigationHealthView(HomeAssistantView):
    """Read-only JSON status feed for the LLM irrigation monitor."""

    url = f"/api/{DOMAIN}/health.json"
    name = f"{DOMAIN}:health_json"
    requires_auth = True

    async def get(self, request):
        from aiohttp import web
        from homeassistant.util import dt as dt_util

        from . import find_coordinator

        hass: HomeAssistant = request.app["hass"]
        # Admin-only, matching the v1.17.10 WS require_admin decision: schedule /
        # plant / run-time data leaks occupancy patterns, so a non-admin authed
        # user must not enumerate it over HTTP. The LLM monitor authenticates with
        # an ADMIN long-lived token (the owner's).
        user = request.get("hass_user")
        if user is None or not getattr(user, "is_admin", False):
            from homeassistant.exceptions import Unauthorized

            raise Unauthorized()
        coord = find_coordinator(hass)
        if coord is None:
            return web.json_response(
                {
                    "schema_version": _SCHEMA_VERSION,
                    "ok": False,
                    "error": "no coordinator configured",
                },
                status=503,
            )

        now = dt_util.now()

        # Next planned run per schedule (raw scheduled intent — earliest start).
        planned = next_runs(
            coord.schedule_store.enabled_schedules(),
            from_dt=now,
            until_dt=now + timedelta(days=_NEXT_RUN_LOOKAHEAD_DAYS),
            sun_times=coord.sun_times,
        )
        next_by_sched: dict[str, str] = {}
        for r in planned:
            next_by_sched.setdefault(r.schedule_id, r.start_at.isoformat())

        schedules = [
            {
                "id": s.id,
                "name": s.name,
                "enabled": s.enabled,
                "zones": [step.zone_entity_id for step in s.all_steps()],
                "duration_minutes": s.duration_minutes,
                "cadence": _cadence_summary(s),
                "next_run_at": next_by_sched.get(s.id),
            }
            for s in coord.schedule_store.all()
        ]

        # Plants + drips/volume per loop (WUCOLS x ETo design). v1.28 —
        # effective_eto() is the auto FAO-56 figure when enabled+fresh, else manual.
        eto = float(coord.effective_eto())
        efficiency = float(coord.config.get("drip_efficiency", DEFAULT_EFFICIENCY))
        try:
            loops = [
                serialize_loop_report(rep)
                for rep in build_yard_report(
                    coord.plants.all(), coord.schedule_store.all(), eto, efficiency
                )
            ]
        except Exception as err:  # the health feed must never 500 on design math
            _LOGGER.debug("yard report failed for health feed: %s", err)
            loops = []

        history = coord.run_history.all()
        recent = [rec.to_dict() for rec in history[:_RECENT_RUNS]]
        running = [
            {
                "zone": rec.zone_entity_id,
                "zone_name": rec.zone_name,
                "schedule": rec.schedule_name,
                "source": rec.source,
                "started_at": rec.started_at,
                "requested_minutes": rec.requested_minutes,
            }
            for rec in coord.run_history.running_records()
        ]

        issues, aborted, skipped, short = _detect_misses(history, now)

        # v1.33 — per-plant vision-health: prior verdict, whether the plant is due for
        # a (re)assessment, and the exact photos to review. The external vision job
        # reads `due_for_review` to know what to assess and `review` for the image URLs.
        from .vision_health import due_for_review, select_review_photos

        now_iso = now.isoformat()
        plant_health = [
            {
                "plant_id": p.id,
                "name": p.name,
                "photo_count": len(p.photos),
                "health": p.health,
                "due_for_review": bool(p.photos)
                and due_for_review((p.health or {}).get("assessed_at"), now_iso),
                "review": select_review_photos(list(p.photos)),
                # v1.36 — context for the vision job's prompt: species + optimal lux
                # range + the latest light-survey verdict (all rail-bounded values).
                "species": p.species,
                "lux_range": (
                    {"low": p.lux_low, "high": p.lux_high} if p.lux_low is not None else None
                ),
                "light": (dict(p.light_surveys[0]) if p.light_surveys else None),
            }
            for p in coord.plants.all()
        ]

        return web.json_response(
            {
                "schema_version": _SCHEMA_VERSION,
                "generated_at": now.isoformat(),
                "controller": {
                    "busy": bool(running),
                    "running": running,
                    "lockout_until": (
                        coord.lockout_until.isoformat() if coord.lockout_until else None
                    ),
                },
                "schedules": schedules,
                "loops": loops,
                "eto": coord.eto_status(),
                # v1.32 — advisory daily plan (today's runs prioritized by urgency,
                # with skip hints for saturated zones). The LLM advisor reads this
                # and may propose a re-ordering, validated against the rail.
                "daily_plan": serialize_daily_plan(coord.build_today_plan(now)),
                "plant_health": plant_health,
                "recent_runs": recent,
                "health": {
                    "ok": not issues,
                    "window_hours": _MISS_WINDOW_HOURS,
                    "aborted": aborted,
                    "skipped": skipped,
                    "short": short,
                    "issues": issues,
                },
            }
        )


def async_register_health_view(hass: HomeAssistant) -> None:
    """Register the health JSON view. Idempotent."""
    if hass.http is None:
        return
    hass.http.register_view(IrrigationHealthView())
