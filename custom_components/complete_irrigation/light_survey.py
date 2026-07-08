"""Light-survey rail (v1.35) — per-plant lux profiling with a roaming sensor.

Modeled on the "Light Meter" pattern from consumer plant apps: each plant gets an
optimal-lux range, a portable illuminance sensor (e.g. an ESP32/VEML7700 Zigbee
stake) is placed at the plant, the coordinator samples it for a timed window, and
the summarized reading is verdicted against the plant's range ("too little light /
optimal / too much") and stored on the plant record.

Pure + HA-free: the coordinator does the actual sensor reads on its tick; this
module owns validation, summarization, verdicts, and the bounded stored entry —
so a flaky sensor or crafted state can never poison .storage or the panel.
"""

from __future__ import annotations

import math
from typing import Any

# Full sunlight is ~100-120k lux; leave headroom but reject absurdities.
MAX_LUX = 200_000
# Survey window bounds (minutes). The coordinator samples once per tick (~60 s),
# so a 10-minute survey collects ~10 samples — plenty for a placement reading.
MIN_SURVEY_MINUTES = 1
MAX_SURVEY_MINUTES = 240
DEFAULT_SURVEY_MINUTES = 10
# Newest-first history cap on the plant record (mirrors the photo cap's intent:
# bounded storage no matter how enthusiastic the surveyor).
MAX_SURVEYS_KEPT = 20

VERDICT_TOO_LOW = "too_low"
VERDICT_OPTIMAL = "optimal"
VERDICT_TOO_HIGH = "too_high"
VERDICT_NO_RANGE = "no_range"  # survey taken, but the plant has no lux range set

_VERDICTS = (VERDICT_TOO_LOW, VERDICT_OPTIMAL, VERDICT_TOO_HIGH, VERDICT_NO_RANGE)


def validate_lux_range(low: Any, high: Any) -> tuple[int, int]:
    """Validate a plant's optimal-lux range. Returns (low, high) as ints.

    Raises ValueError on non-finite, non-positive, out-of-bound, or inverted
    input — the service boundary surfaces that to the caller.
    """
    try:
        low_f, high_f = float(low), float(high)
    except (TypeError, ValueError) as err:
        raise ValueError(f"lux range must be numeric, got {low!r}/{high!r}") from err
    if not math.isfinite(low_f) or not math.isfinite(high_f):
        raise ValueError("lux range must be finite")
    low_i, high_i = int(low_f), int(high_f)
    if not (0 < low_i < high_i <= MAX_LUX):
        raise ValueError(
            f"lux range must satisfy 0 < low < high <= {MAX_LUX}, got {low_i}..{high_i}"
        )
    return low_i, high_i


def summarize_samples(samples: list[Any]) -> dict[str, Any] | None:
    """Reduce raw tick samples to {lux_min, lux_avg, lux_max, samples}.

    Non-numeric / non-finite / negative / absurd values are dropped (an
    'unavailable' sensor tick must not zero the average). Returns None when
    nothing usable was collected — the caller reports a failed survey rather
    than storing a lie.
    """
    clean: list[float] = []
    for s in samples:
        try:
            v = float(s)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(v) or v < 0 or v > MAX_LUX:
            continue
        clean.append(v)
    if not clean:
        return None
    return {
        "lux_min": round(min(clean), 1),
        "lux_avg": round(sum(clean) / len(clean), 1),
        "lux_max": round(max(clean), 1),
        "samples": len(clean),
    }


def light_verdict(avg_lux: float, lux_low: int | None, lux_high: int | None) -> str:
    """Verdict the average reading against the plant's optimal range."""
    if lux_low is None or lux_high is None:
        return VERDICT_NO_RANGE
    if avg_lux < lux_low:
        return VERDICT_TOO_LOW
    if avg_lux > lux_high:
        return VERDICT_TOO_HIGH
    return VERDICT_OPTIMAL


def verdict_text(verdict: str, plant_name: str) -> str:
    """Plain-language wording for notifications + the panel badge."""
    if verdict == VERDICT_TOO_LOW:
        return f"Too little light for {plant_name} at this spot."
    if verdict == VERDICT_TOO_HIGH:
        return f"More light than {plant_name} prefers at this spot."
    if verdict == VERDICT_OPTIMAL:
        return f"Light level is in {plant_name}'s optimal range here."
    return f"Survey stored for {plant_name} — set an optimal lux range to get a verdict."


def make_survey_entry(
    *,
    ts: int,
    minutes: int,
    sensor_entity_id: str,
    summary: dict[str, Any],
    verdict: str,
) -> dict[str, Any]:
    """The bounded dict stored newest-first on PlantRecord.light_surveys."""
    if verdict not in _VERDICTS:
        raise ValueError(f"unknown verdict {verdict!r}")
    return {
        "ts": int(ts),
        "minutes": int(minutes),
        "sensor": str(sensor_entity_id)[:120],
        "lux_min": summary["lux_min"],
        "lux_avg": summary["lux_avg"],
        "lux_max": summary["lux_max"],
        "samples": summary["samples"],
        "verdict": verdict,
    }


def clean_stored_surveys(raw: Any) -> tuple[dict[str, Any], ...]:
    """Load-path guard for PlantRecord.from_dict: keep only well-formed entries
    (finite numerics, known verdict), newest-first, capped. A hand-tampered
    .storage file degrades to fewer entries, never to a crash or poisoned math."""
    if not isinstance(raw, (list, tuple)):
        return ()
    out: list[dict[str, Any]] = []
    for e in raw:
        if not isinstance(e, dict):
            continue
        try:
            ts = int(e["ts"])
            minutes = int(e["minutes"])
            nums = {k: float(e[k]) for k in ("lux_min", "lux_avg", "lux_max")}
            samples = int(e["samples"])
            verdict = str(e["verdict"])
        except (KeyError, TypeError, ValueError):
            continue
        if verdict not in _VERDICTS or samples <= 0:
            continue
        if any(not math.isfinite(v) or v < 0 or v > MAX_LUX for v in nums.values()):
            continue
        out.append(
            {
                "ts": ts,
                "minutes": minutes,
                "sensor": str(e.get("sensor", ""))[:120],
                **{k: round(v, 1) for k, v in nums.items()},
                "samples": samples,
                "verdict": verdict,
            }
        )
    out.sort(key=lambda d: d["ts"], reverse=True)
    return tuple(out[:MAX_SURVEYS_KEPT])
