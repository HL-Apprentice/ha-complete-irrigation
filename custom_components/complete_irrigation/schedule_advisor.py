"""Schedule-rearrangement proposals — pure validation rail, HA-free (v1.56).

The LLM scheduler is PROPOSE-ONLY: it suggests how to rearrange schedules so
everything fits, and the user taps to apply. This module bounds that reply so an
approved item can never drop a run or invent/lose water. Two shapes:

  * shift  — move a schedule's base start time to a new HH:MM.
  * split  — break one schedule into timed parts whose minutes sum EXACTLY to its
             current duration (no water lost or added), each part >= its chunk floor.

Anything malformed, referencing an unknown schedule, or that would change total
water is dropped. Mirrors watering_advisor.validate_advice's defensive style.
"""

from __future__ import annotations

import re
from typing import Any

TYPE_SHIFT = "shift"
TYPE_SPLIT = "split"
MAX_ITEMS = 12
MAX_PARTS = 8

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _clean_start(v: Any) -> str | None:
    s = str(v or "").strip()
    return s if _HHMM_RE.match(s) else None


def _clean_reason(v: Any) -> str:
    return str(v or "").strip()[:200]


def validate_schedule_advice(
    raw: Any, *, schedules_by_id: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    """Bound a raw LLM reply into stored schedule advice, or None if unusable.

    schedules_by_id maps schedule id -> {"duration_minutes": int,
    "min_chunk": int, "name": str}. Returns {"items": [...], "summary": str} with
    at most MAX_ITEMS validated items, or None when nothing survives.
    """
    if not isinstance(raw, dict):
        return None
    items_raw = raw.get("items")
    if not isinstance(items_raw, list):
        return None

    out: list[dict[str, Any]] = []
    for it in items_raw:
        if len(out) >= MAX_ITEMS or not isinstance(it, dict):
            continue
        sid = str(it.get("schedule_id") or "")
        meta = schedules_by_id.get(sid)
        if meta is None:  # unknown schedule -> drop
            continue
        kind = it.get("type")
        if kind == TYPE_SHIFT:
            start = _clean_start(it.get("proposed_start"))
            if start is None:
                continue
            out.append(
                {
                    "type": TYPE_SHIFT,
                    "schedule_id": sid,
                    "schedule_name": meta.get("name", sid),
                    "proposed_start": start,
                    "reason": _clean_reason(it.get("reason")),
                }
            )
        elif kind == TYPE_SPLIT:
            parts = _validate_parts(it.get("parts"), meta)
            if parts is None:
                continue
            out.append(
                {
                    "type": TYPE_SPLIT,
                    "schedule_id": sid,
                    "schedule_name": meta.get("name", sid),
                    "parts": parts,
                    "reason": _clean_reason(it.get("reason")),
                }
            )
        # unknown type -> drop
    if not out:
        return None
    return {"items": out, "summary": _clean_reason(raw.get("summary"))}


def _validate_parts(parts_raw: Any, meta: dict[str, Any]) -> list[dict[str, Any]] | None:
    """A split's parts must be 2..MAX_PARTS timed slices, each a valid HH:MM start
    with minutes >= the chunk floor, summing EXACTLY to the schedule's duration."""
    if not isinstance(parts_raw, list) or not (2 <= len(parts_raw) <= MAX_PARTS):
        return None
    duration = int(meta.get("duration_minutes") or 0)
    floor = max(1, int(meta.get("min_chunk") or 1))
    cleaned: list[dict[str, Any]] = []
    total = 0
    for p in parts_raw:
        if not isinstance(p, dict):
            return None
        start = _clean_start(p.get("start"))
        try:
            minutes = int(p.get("minutes"))
        except (TypeError, ValueError):
            return None
        if start is None or minutes < floor:
            return None
        total += minutes
        cleaned.append({"start": start, "minutes": minutes})
    if total != duration:  # never lose or add water
        return None
    return cleaned
