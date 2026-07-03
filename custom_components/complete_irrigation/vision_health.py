"""Plant vision-health — pure-logic core for the biannual photo health check (v1.33).

A local vision model (on the NAS RTX 5060) looks at a plant's recent photos against
an earlier baseline and returns a structured health verdict. This module is the
HA-free, unit-testable core:

  * ``select_review_photos`` — pick the (baseline, latest) photo pair to compare, so
    the model can reason about CHANGE over time (biannual by design).
  * ``validate_verdict`` — the deterministic RAIL: parse + bound the model's JSON so a
    hallucinating or malformed vision model can never (a) inject an actuation
    instruction dressed up as "care", (b) store unbounded / non-string garbage, or
    (c) drive watering. It's advisory only. Off-spec output is normalized or dropped.
  * ``HealthReport`` / ``serialize_report`` — the stored, JSON-safe per-plant result.

ADVISORY ONLY — nothing here changes watering; it produces a report + optional concern
flag surfaced to the user. Deterministic rails gate the model exactly like
daily_planner's ``reconcile_llm_ordering`` gates the scheduler advisor.

Pure + HA-free (like hydraulics / et_calc / daily_planner).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime

# Allowed health states (anything else from the model -> "unknown").
HEALTH_STATES = ("thriving", "healthy", "stressed", "declining", "unknown")
_DEFAULT_STATE = "unknown"

_MAX_CARE_ITEMS = 6
_MAX_CONCERNS = 6
_MAX_ITEM_CHARS = 160
_MAX_TEXT_CHARS = 600
_DEFAULT_CONFIDENCE = 0.5

# A "care suggestion" must be advisory prose, never a control instruction. Drop any
# item that smells like an actuation command / service call / entity target, so a
# hallucinating model can't surface "turn on switch.citrus" or "call run_zone" as
# guidance the user might act on blindly (or that a future automation might parse).
_ACTUATION_RE = re.compile(
    r"(turn\s+(on|off)|run_zone|switch\.|service|call\s+\w+\.\w+|set\s+\w+\s*=|"
    r"\b(activate|energize|engage)\b|"
    r"\b(start|open|enable|trigger|begin|run)\s+(the\s+)?(zone|valve|schedule|irrigation|watering)\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class HealthReport:
    plant_id: str
    assessed_at: str  # ISO timestamp (stamped by the caller)
    health_state: str
    confidence: float  # 0..1
    changes_since_last: str
    concerns: tuple[str, ...]
    suggested_care: tuple[str, ...]
    baseline_ts: int | None
    latest_ts: int | None
    model: str


def select_review_photos(photos, *, min_gap_days: int = 60) -> dict | None:
    """From a plant's photo history (newest-first list of ``{ts, path}``), pick what to
    review: the latest photo, plus a baseline to compare against (the OLDEST photo, so
    change over time is visible) when there's a second photo at least ``min_gap_days``
    older. Returns ``{"latest": {...}, "baseline": {...}|None}`` or None if there are no
    usable photos. Pure — the caller does the actual image I/O + model call."""
    usable = [
        p
        for p in (photos or [])
        if isinstance(p, dict) and isinstance(p.get("ts"), int) and p.get("path")
    ]
    if not usable:
        return None
    usable.sort(key=lambda p: p["ts"], reverse=True)
    latest = usable[0]
    baseline = None
    oldest = usable[-1]
    if oldest is not latest and (latest["ts"] - oldest["ts"]) >= min_gap_days * 86400:
        baseline = oldest
    return {"latest": latest, "baseline": baseline}


def _clean_text(v, limit: int) -> str:
    if not isinstance(v, str):
        return ""
    return v.strip()[:limit]


def _clean_items(v, *, cap: int) -> tuple[str, ...]:
    if not isinstance(v, (list, tuple)):
        return ()
    out: list[str] = []
    for item in v:
        s = _clean_text(item, _MAX_ITEM_CHARS)
        if not s:
            continue
        if _ACTUATION_RE.search(s):
            continue  # advisory guidance only — never an actuation instruction
        out.append(s)
        if len(out) >= cap:
            break
    return tuple(out)


def validate_verdict(
    raw,
    *,
    plant_id: str,
    latest_ts: int | None,
    baseline_ts: int | None,
    model: str,
    now_iso: str,
) -> HealthReport | None:
    """The RAIL. Turn the vision model's raw dict into a bounded, safe HealthReport, or
    None if it's unusable. Normalizes an out-of-enum state to "unknown", clamps
    confidence to [0, 1], truncates text, caps list lengths, and STRIPS any care /
    concern item that reads like a control instruction (advisory only). The model can
    inform the user; it can never smuggle in an action or unbounded payload."""
    if not isinstance(raw, dict):
        return None
    state = raw.get("health_state")
    if not isinstance(state, str) or state.strip().lower() not in HEALTH_STATES:
        state = _DEFAULT_STATE
    else:
        state = state.strip().lower()
    conf = raw.get("confidence", _DEFAULT_CONFIDENCE)
    try:
        conf = float(conf)
    except (TypeError, ValueError):
        conf = _DEFAULT_CONFIDENCE
    if not math.isfinite(conf):
        # NaN/Inf would sail past the < / > clamps below and get stored — then
        # json.dumps emits `NaN`, which is invalid JSON and corrupts the .storage
        # write. Mirror plant.py's isfinite discipline.
        conf = _DEFAULT_CONFIDENCE
    conf = 0.0 if conf < 0.0 else 1.0 if conf > 1.0 else round(conf, 2)
    return HealthReport(
        plant_id=str(plant_id),
        assessed_at=now_iso,
        health_state=state,
        confidence=conf,
        changes_since_last=_clean_text(raw.get("changes_since_last"), _MAX_TEXT_CHARS),
        concerns=_clean_items(raw.get("concerns"), cap=_MAX_CONCERNS),
        suggested_care=_clean_items(raw.get("suggested_care"), cap=_MAX_CARE_ITEMS),
        baseline_ts=baseline_ts if isinstance(baseline_ts, int) else None,
        latest_ts=latest_ts if isinstance(latest_ts, int) else None,
        model=_clean_text(model, 80),
    )


def due_for_review(last_assessed_iso, now_iso, interval_days: int = 182) -> bool:
    """True if a plant is due for a (re)assessment: never assessed (empty/None), or its
    last assessment is >= interval_days old (biannual by default). An unparseable or
    tz-naive timestamp returns True — fail toward reviewing rather than silently never
    checking. The health feed uses this to tell the NAS job which plants to look at."""
    if not last_assessed_iso:
        return True
    try:
        last = datetime.fromisoformat(last_assessed_iso)
        now = datetime.fromisoformat(now_iso)
    except (TypeError, ValueError):
        return True
    if last.tzinfo is None or now.tzinfo is None:
        return True
    return (now - last).total_seconds() >= interval_days * 86400


def report_has_concern(report: HealthReport) -> bool:
    """True when the report warrants surfacing a notification: a non-thriving/healthy
    state, or any explicit concern. Drives the optional 'plant needs attention' alert."""
    return bool(report.concerns) or report.health_state in ("stressed", "declining")


def serialize_report(report: HealthReport) -> dict:
    """JSON-safe dict for .storage / health.json / ws_api / the panel."""
    return {
        "plant_id": report.plant_id,
        "assessed_at": report.assessed_at,
        "health_state": report.health_state,
        "confidence": report.confidence,
        "changes_since_last": report.changes_since_last,
        "concerns": list(report.concerns),
        "suggested_care": list(report.suggested_care),
        "baseline_ts": report.baseline_ts,
        "latest_ts": report.latest_ts,
        "model": report.model,
        "needs_attention": report_has_concern(report),
    }
