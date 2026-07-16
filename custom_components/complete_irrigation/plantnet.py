"""Pl@ntNet photo identification — pure response parsing, HA-free.

Pl@ntNet (my.plantnet.org) is a purpose-built plant-identification API — a
**no-LLM** way to identify a plant from a photo. The free tier allows 500
identifications/day for non-commercial use with a free API key. This module
parses its response; the multipart image upload lives in services.py.

We take only the NAME from Pl@ntNet — the water-use / care still comes from the
curated table (plant_care_db), the same as every other identification path.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

_IDENTIFY = "https://my-api.plantnet.org/v2/identify/all"


def plantnet_url(api_key: str) -> str:
    """Pl@ntNet identify-all endpoint with the key as a query parameter, plus a
    handful of languages for common names. The image goes in the multipart body."""
    return f"{_IDENTIFY}?api-key={quote((api_key or '').strip())}&lang=en&nb-results=5"


def parse_plantnet(payload: Any) -> dict[str, Any]:
    """Normalize a Pl@ntNet identify response.

    Returns:
      matched      - True when a species was returned
      species      - top scientific name ("Nerium oleander"), or ""
      common_name  - top English common name, or ""
      confidence   - top result score 0-1
      alternatives - up to 3 other scientific names
      remaining    - remaining identifications today (int) or None
    """
    out = {
        "matched": False,
        "species": "",
        "common_name": "",
        "confidence": 0.0,
        "alternatives": [],
        "remaining": None,
    }
    if not isinstance(payload, dict):
        return out
    rem = payload.get("remainingIdentificationRequests")
    if isinstance(rem, (int, float)):
        out["remaining"] = int(rem)
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        return out
    top = results[0] if isinstance(results[0], dict) else {}
    species = top.get("species") if isinstance(top.get("species"), dict) else {}
    name = str(species.get("scientificNameWithoutAuthor") or "").strip()
    if not name:
        return out
    commons = species.get("commonNames")
    out["species"] = name
    out["common_name"] = str(commons[0]) if isinstance(commons, list) and commons else ""
    try:
        out["confidence"] = round(float(top.get("score") or 0.0), 3)
    except (TypeError, ValueError):
        out["confidence"] = 0.0
    for r in results[1:4]:
        if not isinstance(r, dict):
            continue
        sp = r.get("species") if isinstance(r.get("species"), dict) else {}
        alt = str(sp.get("scientificNameWithoutAuthor") or "").strip()
        if alt and alt != name and alt not in out["alternatives"]:
            out["alternatives"].append(alt)
    out["matched"] = True
    return out
