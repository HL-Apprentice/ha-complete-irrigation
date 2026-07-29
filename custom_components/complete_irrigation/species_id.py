"""Species-identification rail (v1.37) — photo → species + care-sheet SUGGESTION.

The consumer plant apps identify a species from a photo and populate the whole
care sheet. Ours does it locally: the integration sends a plant's newest photo to
a user-configured OpenAI-compatible vision endpoint (e.g. Qwen2.5-VL on the NAS
GPU), and THIS rail bounds the model's reply into a SpeciesSuggestion before it
is stored. Nothing is applied automatically — the suggestion sits on the plant
until the user applies it (species + lux range + optional care-plan seed) or
dismisses it. Advisory only; watering is untouched.

Pure + HA-free, same discipline as vision_health.py: a hostile or hallucinating
model can never plant unbounded text, non-finite numbers, or an unknown enum in
.storage.
"""

from __future__ import annotations

import math
import re
import unicodedata
from typing import Any

from .care_tasks import CARE_PLAN_PRESETS
from .hydraulics import WUCOLS_FACTORS
from .light_survey import MAX_LUX

# Sunlight classes (the consumer-app vocabulary) → optimal-lux presets. Applying a
# suggestion maps its sunlight class onto the plant's lux range for light surveys.
SUNLIGHT_LUX: dict[str, tuple[int, int]] = {
    "full_sun": (32000, 100000),
    "partial_sun": (10000, 32000),
    "bright_shade": (3000, 10000),
    "deep_shade": (500, 3000),
}

_MAX_NAME_CHARS = 120
_MAX_NOTE_CHARS = 240
_TEMP_F_MIN, _TEMP_F_MAX = -60.0, 140.0
_DEFAULT_CONFIDENCE = 0.5

_CONTROL_RE = re.compile(
    "[\\x00-\\x1f\\x7f-\\x9f\\u2028\\u2029\\u200e\\u200f\\u202a-\\u202e\\u2066-\\u2069]"
)


def _clean_text(v: Any, limit: int) -> str:
    if not isinstance(v, str):
        return ""
    s = _CONTROL_RE.sub(" ", unicodedata.normalize("NFKC", v)).strip()
    return s[:limit]


def _clean_float(v: Any, lo: float, hi: float) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or not (lo <= f <= hi):
        return None
    return f


def _clean_int(v: Any, lo: int, hi: int) -> int | None:
    f = _clean_float(v, lo, hi)
    return None if f is None else int(f)


def _first_enum(v: Any) -> Any:
    """A small vision model sometimes echoes the whole pipe list of options
    (`"full_sun|bright_shade"`) instead of picking one. Take the first token."""
    return v.split("|", 1)[0].strip() if isinstance(v, str) and "|" in v else v


def extract_suggestion_json(text: Any) -> dict | None:
    """Pull the JSON object out of a vision model's reply, repairing the mistakes
    small models make so a near-miss still identifies the plant.

    v1.62 — the repair logic now lives in llm_json.extract_json_object so the
    scheduling path shares it instead of carrying a weaker copy. Kept as a named
    entry point because that is what this module's callers and tests use.
    """
    from .llm_json import extract_json_object

    return extract_json_object(text)


def validate_suggestion(raw: Any, *, model: str, now_iso: str) -> dict[str, Any] | None:
    """Bound a raw model reply into a stored suggestion dict, or None if unusable.

    Required: a non-empty species. Everything else optional and dropped when out
    of bounds — a partial suggestion is still useful; a lie-shaped one is not.
    """
    if not isinstance(raw, dict):
        return None
    species = _clean_text(raw.get("species"), _MAX_NAME_CHARS)
    if not species:
        return None

    out: dict[str, Any] = {
        "species": species,
        "common_name": _clean_text(raw.get("common_name"), _MAX_NAME_CHARS),
        "confidence": _clean_float(raw.get("confidence"), 0.0, 1.0),
        "identified_at": _clean_text(str(now_iso), 40),
        "model": _clean_text(model, 80),
        "note": _clean_text(raw.get("note"), _MAX_NOTE_CHARS),
    }
    if out["confidence"] is None:
        out["confidence"] = _DEFAULT_CONFIDENCE

    sunlight = _first_enum(raw.get("sunlight_class"))
    out["sunlight_class"] = (
        sunlight if isinstance(sunlight, str) and sunlight in SUNLIGHT_LUX else None
    )

    # Temperature tolerance (°F), both-or-neither with sane ordering.
    t_lo = _clean_float(raw.get("temp_low_f"), _TEMP_F_MIN, _TEMP_F_MAX)
    t_hi = _clean_float(raw.get("temp_high_f"), _TEMP_F_MIN, _TEMP_F_MAX)
    if t_lo is not None and t_hi is not None and t_lo < t_hi:
        out["temp_low_f"], out["temp_high_f"] = round(t_lo), round(t_hi)
    else:
        out["temp_low_f"] = out["temp_high_f"] = None

    wucols = _first_enum(raw.get("wucols_category"))
    out["wucols_category"] = (
        wucols if isinstance(wucols, str) and wucols in WUCOLS_FACTORS else None
    )

    preset = _first_enum(raw.get("care_plan_preset"))
    out["care_plan_preset"] = (
        preset if isinstance(preset, str) and preset in CARE_PLAN_PRESETS else None
    )

    # Cadences: watering (days between waterings, seasonal ballpark) and
    # fertilizing (days; 0 = not necessary).
    out["water_every_days"] = _clean_int(raw.get("water_every_days"), 1, 60)
    out["fertilize_every_days"] = _clean_int(raw.get("fertilize_every_days"), 0, 3650)
    # v1.38 — canopy-footprint estimate from the photo (sq ft, rough; editable).
    canopy = _clean_float(raw.get("canopy_area_sqft"), 0.5, 10000)
    out["canopy_area_sqft"] = round(canopy, 1) if canopy is not None else None
    return out


def clean_stored_suggestion(raw: Any) -> dict[str, Any] | None:
    """Load-path guard for PlantRecord.from_dict — re-bound whatever .storage
    holds (hand-tampered files degrade to None, never to a crash)."""
    if not isinstance(raw, dict):
        return None
    try:
        return validate_suggestion(
            raw,
            model=str(raw.get("model") or ""),
            now_iso=str(raw.get("identified_at") or ""),
        )
    except (TypeError, ValueError):
        # Documented contract: tampered files degrade to None, never to a crash
        # (a crash here would drop the WHOLE plant record on load — rail violation).
        return None


def suggested_lux_range(suggestion: dict[str, Any]) -> tuple[int, int] | None:
    """The lux range a suggestion's sunlight class maps to (bounded by MAX_LUX)."""
    cls = suggestion.get("sunlight_class")
    rng = SUNLIGHT_LUX.get(cls) if isinstance(cls, str) else None
    if rng is None:
        return None
    low, high = rng
    return (max(1, low), min(MAX_LUX, high))
