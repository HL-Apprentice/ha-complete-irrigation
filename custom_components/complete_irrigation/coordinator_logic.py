"""Pure-logic helpers used by ScheduleCoordinator (no HA dependency).

Extracted from coordinator.py in v1.16 so the HA-coupled orchestration
file shrinks to just lifecycle + tick + event firing. Every function
here takes simple Python values (dicts, lists, callables) and returns
strings/lists/sets — they're all directly unit-tested without standing
up a real HA instance.

Re-exported via coordinator.py to preserve every existing import path.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime
from typing import Any


def plan_session_resume(
    sessions: dict[str, dict],
    now: datetime,
    *,
    min_resume_seconds: int = 60,
    hard_cap_minutes: int = 480,
) -> list[dict]:
    """v1.30 restart fail-over — decide, per persisted active-run session, whether
    to RESUME it (the run was interrupted by an HA restart and still has time left
    before its scheduled end) or CLOSE it (it would have finished during the
    outage). Pure; the coordinator does the actual HA I/O.

    Each session carries at least ``deadline`` (ISO timestamp of the run's planned
    end) and optionally ``total_minutes``. Returns one dict per zone:
        {zone, action: "resume"|"close", remaining_minutes, session}.
    A session with a missing/unparseable deadline (or a tz mismatch vs ``now``) is
    CLOSED — fail safe: turn the valve off rather than resume on bad data."""
    out: list[dict] = []
    for zone, s in sorted(sessions.items()):
        s = s or {}
        try:
            deadline = datetime.fromisoformat(s["deadline"])
            remaining = (deadline - now).total_seconds()
        except (KeyError, TypeError, ValueError):
            out.append({"zone": zone, "action": "close", "remaining_minutes": 0, "session": s})
            continue
        if remaining > min_resume_seconds:
            total = s.get("total_minutes")
            cap = int(total) if isinstance(total, int | float) and total > 0 else hard_cap_minutes
            remaining_min = max(1, min(cap, math.ceil(remaining / 60)))
            out.append(
                {
                    "zone": zone,
                    "action": "resume",
                    "remaining_minutes": remaining_min,
                    "session": s,
                }
            )
        else:
            out.append({"zone": zone, "action": "close", "remaining_minutes": 0, "session": s})
    return out


def build_weekly_zone_summary(
    zones_iter,
    schedules,
    zones_config: dict,
    get_state: Callable[[str], Any],
) -> list[str]:
    """PRD #80 — pure-logic for the per-zone weekly digest.

    Returns a list of human-readable bullet lines, one per configured
    zone. Each line shows: zone name, schedule count, current moisture
    (if any sensor is bound), and the configured target.

    `zones_iter` is the iterable of zone entity_ids (from the entry).
    `schedules` is the enabled-schedule list.
    `zones_config` is `coord.config["zones"]` — keyed by entity_id.
    `get_state(eid)` returns a HA state-like object (or None) — same
    contract used by detect_sensor_offline_transitions().
    """
    out: list[str] = []
    for entity_id in zones_iter:
        # Count schedules that include this zone (top-level OR a step).
        n_scheds = 0
        for s in schedules:
            if s.zone_entity_id == entity_id:
                n_scheds += 1
                continue
            steps = getattr(s, "zone_steps", None) or ()
            if any(st.zone_entity_id == entity_id for st in steps):
                n_scheds += 1

        # Resolve friendly name (state attrs > entity_id fallback)
        zone_state = get_state(entity_id)
        friendly = (
            zone_state.attributes.get("friendly_name")
            if zone_state and hasattr(zone_state, "attributes")
            else None
        ) or entity_id

        # Moisture: average the bound sensors (if any).
        zcfg = zones_config.get(entity_id, {})
        sensors = zcfg.get("moisture_entities") or []
        target = zcfg.get("target_pct")
        moistures: list[float] = []
        for eid in sensors:
            s = get_state(eid)
            if s is None or s.state in ("unknown", "unavailable"):
                continue
            try:
                moistures.append(float(s.state))
            except (TypeError, ValueError):
                continue

        bits = [f"• {friendly}: {n_scheds} schedule(s)"]
        if moistures:
            avg = sum(moistures) / len(moistures)
            if target is not None:
                bits.append(f"moisture {avg:.0f}% (target {int(target)}%)")
            else:
                bits.append(f"moisture {avg:.0f}%")
        elif sensors:
            bits.append("moisture sensors offline")
        out.append(", ".join(bits))
    return out


def compute_expired_establishment_schedules(
    schedules: list, today, already_prompted: set[str]
) -> list:
    """Pure-logic for PRD #76.

    Returns the list of schedules whose `end_date` is on or before `today`
    AND whose id is not in `already_prompted`. Used by the coordinator's
    daily check to fire a "what's next?" prompt exactly once per
    establishment window completion.
    """
    out = []
    for s in schedules:
        end_date = getattr(s, "end_date", None)
        if end_date is None:
            continue
        if end_date > today:
            continue
        if s.id in already_prompted:
            continue
        out.append(s)
    return out


def detect_sensor_offline_transitions(
    zones: dict,
    get_state: Callable[[str], Any],
    previously_offline: set[str],
) -> tuple[set[str], set[str]]:
    """Pure-logic for PRD #57.

    Returns (newly_offline, newly_back_online) — the set of bound moisture
    sensor entity_ids whose availability flipped since the last tick.
    Caller is expected to merge `newly_offline` into and remove
    `newly_back_online` from its tracked set after dispatching alerts.

    A sensor is "offline" when its state is None (missing from HA) or
    in {"unknown", "unavailable"}.
    """
    currently_offline: set[str] = set()
    for zone_cfg in zones.values():
        for eid in zone_cfg.get("moisture_entities") or []:
            state = get_state(eid)
            if state is None or state.state in ("unknown", "unavailable"):
                currently_offline.add(eid)
    newly_offline = currently_offline - previously_offline
    newly_back_online = previously_offline - currently_offline
    return newly_offline, newly_back_online


def compute_low_moisture_offenders(zones: dict, get_state: Callable[[str], Any]) -> list[str]:
    """Pure-logic for the daily low-moisture summary.

    Given a per-zone config map and a state-getter callable (entity_id →
    state object with .state and .attributes, or None), return a list
    of human-readable offender strings for any zone whose first below-
    minimum moisture sensor is below the configured min%.
    """
    offenders: list[str] = []
    for zone_id, zone_cfg in zones.items():
        sensors = zone_cfg.get("moisture_entities") or []
        min_pct = zone_cfg.get("min_pct")
        if not sensors or min_pct is None:
            continue
        for eid in sensors:
            state = get_state(eid)
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
    return offenders
