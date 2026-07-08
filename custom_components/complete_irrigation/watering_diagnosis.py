"""Over/under-watering diagnosis rail (v1.35) — Signs → Confirm → Suggestions.

The consumer plant-app disease pages structure every issue as Signs → How to
confirm → Solution → Prevention, with over- and under-watering as first-class
issues. A plant app can only TELL the human; an irrigation controller can act —
so this rail cross-checks the data the integration already has:

  • current zone moisture vs the zone's min/target/max thresholds
  • recent run history (completed / skipped-by-moisture / aborted counts)
  • vision-health concern keywords for the zone's plants (v1.33 reports)

and emits a deterministic, ADVISORY diagnosis card per zone. Same guardrail as
the daily planner: it suggests schedule changes, it never applies them.

Pure + HA-free; the WS layer feeds it live data and the panel renders the card.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .run_history import (
    STATUS_ABORTED,
    STATUS_COMPLETED,
    STATUS_SKIPPED,
    RunHistoryRecord,
)

STATUS_OK = "ok"
STATUS_POSSIBLE_OVER = "possible_overwatering"
STATUS_POSSIBLE_UNDER = "possible_underwatering"
STATUS_UNKNOWN = "unknown"  # no moisture sensors bound -> nothing to diagnose from

# Vision-health concern keywords that corroborate each direction (matched
# case-insensitively against the bounded concern strings the v1.33 rail stored).
_OVER_KEYWORDS = ("yellow", "root rot", "fungus", "mold", "mould", "soggy", "mushy", "drooping")
_UNDER_KEYWORDS = ("wilt", "brown", "crisp", "dry", "scorch", "curl", "brittle")

_LOOKBACK_DAYS = 14


@dataclass(frozen=True)
class ZoneDiagnosis:
    """The advisory card for one zone."""

    zone_entity_id: str
    status: str
    signs: tuple[str, ...] = ()
    confirm: tuple[str, ...] = ()
    suggestions: tuple[str, ...] = ()
    # Raw evidence the panel can show under a details expander.
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "zone_entity_id": self.zone_entity_id,
            "status": self.status,
            "signs": list(self.signs),
            "confirm": list(self.confirm),
            "suggestions": list(self.suggestions),
            "evidence": dict(self.evidence),
        }


def _recent(records: list[RunHistoryRecord], now: datetime) -> list[RunHistoryRecord]:
    cutoff = now - timedelta(days=_LOOKBACK_DAYS)
    out = []
    for r in records:
        try:
            started = datetime.fromisoformat(r.started_at)
        except (TypeError, ValueError):
            continue
        # Tolerate naive/aware mixes defensively: compare only when comparable.
        if (started.tzinfo is None) != (cutoff.tzinfo is None):
            started = started.replace(tzinfo=cutoff.tzinfo)
        if started >= cutoff:
            out.append(r)
    return out


def _concern_hits(concerns: list[str], keywords: tuple[str, ...]) -> list[str]:
    hits = []
    for c in concerns:
        low = c.lower()
        if any(k in low for k in keywords):
            hits.append(c)
    return hits


def diagnose_zone(
    *,
    zone_entity_id: str,
    now: datetime,
    current_pct: float | None,
    min_pct: float | None,
    target_pct: float | None,
    max_pct: float | None,
    history: list[RunHistoryRecord],
    plant_concerns: list[str] | None = None,
) -> ZoneDiagnosis:
    """Deterministic diagnosis from existing signals. Advisory only."""
    concerns = [str(c)[:160] for c in (plant_concerns or []) if isinstance(c, str)][:24]
    recent = _recent([r for r in history if r.zone_entity_id == zone_entity_id], now)
    completed = [r for r in recent if r.status == STATUS_COMPLETED]
    aborted = [r for r in recent if r.status == STATUS_ABORTED]
    moisture_skips = [
        r for r in recent if r.status == STATUS_SKIPPED and "moist" in (r.reason or "").lower()
    ]
    evidence: dict[str, Any] = {
        "lookback_days": _LOOKBACK_DAYS,
        "completed_runs": len(completed),
        "aborted_runs": len(aborted),
        "moisture_skips": len(moisture_skips),
        "current_pct": current_pct,
        "min_pct": min_pct,
        "target_pct": target_pct,
        "max_pct": max_pct,
    }

    have_reading = (
        current_pct is not None
        and isinstance(current_pct, (int, float))
        and math.isfinite(float(current_pct))
    )
    if not have_reading:
        return ZoneDiagnosis(
            zone_entity_id=zone_entity_id,
            status=STATUS_UNKNOWN,
            confirm=("Bind a soil-moisture sensor to this zone to enable watering diagnosis.",),
            evidence=evidence,
        )
    cur = float(current_pct)

    over_signs: list[str] = []
    under_signs: list[str] = []

    if max_pct is not None and cur >= float(max_pct):
        over_signs.append(f"Soil moisture {cur:.0f}% is at/above the zone max ({max_pct:.0f}%).")
    if len(moisture_skips) >= 3:
        over_signs.append(
            f"{len(moisture_skips)} scheduled runs were skipped as already-moist in the last "
            f"{_LOOKBACK_DAYS} days — the soil rarely dries to the watering threshold."
        )
    over_hits = _concern_hits(concerns, _OVER_KEYWORDS)
    if over_hits:
        over_signs.append("Vision health flagged: " + "; ".join(over_hits[:3]) + ".")

    if min_pct is not None and cur < float(min_pct):
        if len(completed) >= 3:
            under_signs.append(
                f"Soil moisture {cur:.0f}% is below the zone minimum ({min_pct:.0f}%) despite "
                f"{len(completed)} completed runs in the last {_LOOKBACK_DAYS} days — water may "
                "not be reaching the roots or the runtime is too short."
            )
        else:
            under_signs.append(
                f"Soil moisture {cur:.0f}% is below the zone minimum ({min_pct:.0f}%)."
            )
    if len(aborted) >= 2:
        under_signs.append(
            f"{len(aborted)} runs were cut short in the last {_LOOKBACK_DAYS} days — "
            "interrupted runs deliver less water than planned."
        )
    under_hits = _concern_hits(concerns, _UNDER_KEYWORDS)
    if under_hits:
        under_signs.append("Vision health flagged: " + "; ".join(under_hits[:3]) + ".")

    # Moisture evidence outranks keyword corroboration: only diagnose a direction
    # the soil actually supports; keywords alone never trip a verdict.
    over_moisture = bool(
        over_signs and ((max_pct is not None and cur >= float(max_pct)) or len(moisture_skips) >= 3)
    )
    under_moisture = bool(min_pct is not None and cur < float(min_pct))

    if over_moisture and not under_moisture:
        return ZoneDiagnosis(
            zone_entity_id=zone_entity_id,
            status=STATUS_POSSIBLE_OVER,
            signs=tuple(over_signs),
            confirm=(
                "Check the soil a knuckle deep near an emitter — constantly wet soil, a musty "
                "smell, or fungus gnats point to overwatering.",
                "Look for yellowing lower leaves or soft, drooping growth on this zone's plants.",
            ),
            suggestions=(
                "Reduce this zone's schedule frequency (add a day between runs) or shorten the "
                "runtime, then re-check in a week.",
                "Raise the zone's moisture-skip threshold review: the gate is already skipping "
                "runs — consider trusting it and lengthening the interval.",
                "Verify the moisture sensor isn't sitting in a low spot that stays wet.",
            ),
            evidence=evidence,
        )
    if under_moisture and not over_moisture:
        return ZoneDiagnosis(
            zone_entity_id=zone_entity_id,
            status=STATUS_POSSIBLE_UNDER,
            signs=tuple(under_signs),
            confirm=(
                "Check the soil a knuckle deep near the plants — bone-dry soil an hour after a "
                "run means water isn't penetrating or isn't reaching them.",
                "Inspect emitters/lines on this zone for clogs, kinks, or popped fittings while "
                "a manual run is active.",
                "Look for wilting, browning leaf edges, or crisping on this zone's plants.",
            ),
            suggestions=(
                "Increase runtime (deep, less frequent watering beats short frequent sips for "
                "trees/shrubs) or add a soak cycle.",
                "If runs keep getting cut short, check the run history for what interrupted them.",
                "Consider the per-plant top-up recommendations in the Yard report for emitter "
                "sizing on this zone.",
            ),
            evidence=evidence,
        )
    if over_moisture and under_moisture:
        # Contradictory soil evidence (e.g. reading below min but max also tripped by
        # stale thresholds) — don't guess; ask for a sensor sanity check.
        return ZoneDiagnosis(
            zone_entity_id=zone_entity_id,
            status=STATUS_UNKNOWN,
            signs=tuple(over_signs + under_signs),
            confirm=(
                "Signals conflict — verify the zone's min/target/max thresholds and that the "
                "moisture sensor is reading sanely before acting.",
            ),
            evidence=evidence,
        )
    return ZoneDiagnosis(
        zone_entity_id=zone_entity_id,
        status=STATUS_OK,
        signs=(),
        confirm=(),
        suggestions=(),
        evidence=evidence,
    )
