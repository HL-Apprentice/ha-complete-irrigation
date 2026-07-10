"""Watering-advisor rail (v1.39) — LLM proposals for times + volumes, bounded.

The last piece of the LLM vision: an external advisor job (on the LLM host)
reads health.json (schedules, per-plant need-vs-delivered, daily plan, ETo),
asks a local text LLM for improvements, and POSTs its RAW advice to
``propose_watering_advice``. THIS rail bounds that advice into typed, capped
proposal items before storage. Nothing is ever applied automatically — each
item carries exactly what the panel needs to apply it through the EXISTING
validated services (update_schedule / update_plant) after the user taps Apply.

Two proposal types only (deliberately narrow):
  * shift_time      — move a schedule's start time (never duration, never skip)
  * emitter_change  — change a plant's installed drip set (count x GPH)

Pure + HA-free, same discipline as vision_health / species_id.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

MAX_ITEMS = 8
_MAX_REASON = 200
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_SAFE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_CONTROL_RE = re.compile(
    "[\\x00-\\x1f\\x7f-\\x9f\\u2028\\u2029\\u200e\\u200f\\u202a-\\u202e\\u2066-\\u2069]"
)

TYPE_SHIFT_TIME = "shift_time"
TYPE_EMITTER_CHANGE = "emitter_change"


def _clean_text(v: Any, limit: int) -> str:
    if not isinstance(v, str):
        return ""
    return _CONTROL_RE.sub(" ", unicodedata.normalize("NFKC", v)).strip()[:limit]


def _valid_id(v: Any) -> str | None:
    if isinstance(v, str) and _SAFE_ID_RE.fullmatch(v):
        return v
    return None


def _clean_item(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    kind = raw.get("type")
    reason = _clean_text(raw.get("reason"), _MAX_REASON)
    if kind == TYPE_SHIFT_TIME:
        sid = _valid_id(raw.get("schedule_id"))
        proposed = raw.get("proposed_start")
        if sid is None or not isinstance(proposed, str) or not _HHMM_RE.fullmatch(proposed):
            return None
        return {
            "type": TYPE_SHIFT_TIME,
            "schedule_id": sid,
            "proposed_start": proposed,
            "reason": reason,
        }
    if kind == TYPE_EMITTER_CHANGE:
        pid = _valid_id(raw.get("plant_id"))
        try:
            count = int(raw.get("proposed_count"))
            gph = float(raw.get("proposed_gph"))
        except (TypeError, ValueError):
            return None
        if pid is None or not (1 <= count <= 100) or not (0.1 <= gph <= 50):
            return None
        return {
            "type": TYPE_EMITTER_CHANGE,
            "plant_id": pid,
            "proposed_count": count,
            "proposed_gph": round(gph, 2),
            "reason": reason,
        }
    return None


def validate_advice(
    raw: Any,
    *,
    model: str,
    now_iso: str,
    known_schedule_ids: set[str],
    known_plant_ids: set[str],
) -> dict[str, Any] | None:
    """Bound a raw advisor reply into a stored advice dict, or None if unusable.

    Items referencing unknown schedules/plants are dropped (a hallucinated id
    must not surface an Apply button that errors). Empty after cleaning -> None.
    """
    if not isinstance(raw, dict):
        return None
    items_raw = raw.get("items")
    if not isinstance(items_raw, (list, tuple)):
        return None
    items: list[dict[str, Any]] = []
    for entry in items_raw[: MAX_ITEMS * 3]:  # tolerate junk without unbounded work
        item = _clean_item(entry)
        if item is None:
            continue
        if item["type"] == TYPE_SHIFT_TIME and item["schedule_id"] not in known_schedule_ids:
            continue
        if item["type"] == TYPE_EMITTER_CHANGE and item["plant_id"] not in known_plant_ids:
            continue
        items.append(item)
        if len(items) >= MAX_ITEMS:
            break
    if not items:
        return None
    return {
        "items": items,
        "summary": _clean_text(raw.get("summary"), 400),
        "model": _clean_text(model, 80),
        "proposed_at": _clean_text(str(now_iso), 40),
    }
