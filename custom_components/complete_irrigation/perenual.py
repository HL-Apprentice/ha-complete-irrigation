"""Perenual plant-care lookup — pure response parsing, HA-free.

Perenual (perenual.com) is a cloud plant database. When the curated care table
(plant_care_db) doesn't cover a species, the "Research details" path can consult
Perenual by NAME as a fallback before reaching for the LLM. The free tier allows
100 requests/day with a free API key.

We map only the fields Perenual reports reliably in its species-list response —
watering and sunlight — onto our own enums; growth-form/preset and temperature
are left unset (Perenual doesn't return them dependably, and a wrong guess
mis-waters the yard). Every value is re-bounded by species_id.validate_suggestion
before it can reach a plant record. The HTTP call lives in services.py.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

_SPECIES_LIST = "https://perenual.com/api/v2/species-list"

# Perenual "watering" category -> (our WUCOLS bucket, a starting cadence in days).
# The cadence is an Arizona-friendly ballpark the user can edit; WUCOLS drives the
# actual irrigation math.
_WATERING_MAP: dict[str, tuple[str, int]] = {
    "frequent": ("high", 3),
    "average": ("moderate", 7),
    "minimum": ("low", 14),
    "none": ("very_low", 30),
}


def perenual_search_url(api_key: str, name: str) -> str:
    """Perenual species-list endpoint, searched by name, key as a query param."""
    key = quote((api_key or "").strip())
    q = quote((name or "").strip())
    return f"{_SPECIES_LIST}?key={key}&q={q}"


def _map_sunlight(raw: Any) -> str | None:
    """Perenual sunlight tokens -> our SUNLIGHT_LUX enum, brightest-wins."""
    if isinstance(raw, str):
        tokens = [raw]
    elif isinstance(raw, list):
        tokens = [str(t) for t in raw]
    else:
        return None
    joined = " ".join(tokens).lower()
    if "full_sun" in joined or "full sun" in joined:
        return "full_sun"
    if "part" in joined or "filtered" in joined:  # part_sun / part_shade / sun-part_shade
        return "partial_sun"
    if "deep" in joined:
        return "deep_shade"
    if "shade" in joined:
        return "bright_shade"
    return None


def parse_perenual_search(payload: Any) -> dict[str, Any]:
    """Normalize a Perenual species-list response into a care suggestion.

    Returns:
      matched      - True when a usable species row was found
      species      - accepted scientific name, or ""
      common_name  - common name, or ""
      confidence   - fixed 0.6 (name-based lookup, not a scored ID)
      sunlight_class / wucols_category / water_every_days - mapped where reliable
      note         - short cycle/source note
    """
    out: dict[str, Any] = {
        "matched": False,
        "species": "",
        "common_name": "",
        "confidence": 0.6,
        "sunlight_class": None,
        "wucols_category": None,
        "water_every_days": None,
        "note": None,
    }
    if not isinstance(payload, dict):
        return out
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        return out
    first = data[0] if isinstance(data[0], dict) else {}

    sci = first.get("scientific_name")
    if isinstance(sci, list) and sci:
        species = str(sci[0] or "").strip()
    elif isinstance(sci, str):
        species = sci.strip()
    else:
        species = ""
    common = str(first.get("common_name") or "").strip()
    if not species:
        species = common
    if not species:
        return out

    watering = str(first.get("watering") or "").strip().lower()
    wucols, water_days = _WATERING_MAP.get(watering, (None, None))

    cycle = str(first.get("cycle") or "").strip()
    note = f"{cycle}; care from Perenual." if cycle else "Care from Perenual."

    out.update(
        {
            "matched": True,
            "species": species,
            "common_name": common,
            "sunlight_class": _map_sunlight(first.get("sunlight")),
            "wucols_category": wucols,
            "water_every_days": water_days,
            "note": note,
        }
    )
    return out
