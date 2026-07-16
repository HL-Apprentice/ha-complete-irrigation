"""Species-name verification against GBIF — pure logic, HA-free.

GBIF (Global Biodiversity Information Facility) publishes an open, keyless
taxonomy API. Its species/match endpoint takes a typed name and returns the
ACCEPTED scientific name, catching typos and resolving common misspellings — so a
user adding a plant by hand can confirm they've got the right name, with **no LLM
and no API key**. This module builds the request URL and parses the response;
the thin async fetch lives in services.py (like the LLM client).

We deliberately do NOT trust GBIF for care/water-use — only for the NAME. Water
use comes from the curated table (plant_care_db).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

_GBIF_MATCH = "https://api.gbif.org/v1/species/match"
# Restrict to plants so an animal/fungus homonym can't hijack a plant name.
_PLANT_KINGDOM_KEY = 6  # GBIF backbone: Plantae


def gbif_match_url(name: str) -> str:
    """The GBIF species/match URL for a typed name (verbose = include near-misses)."""
    n = (name or "").strip()
    return (
        f"{_GBIF_MATCH}?name={quote(n)}&kingdomKey={_PLANT_KINGDOM_KEY}&verbose=true&strict=false"
    )


def parse_gbif_match(payload: Any) -> dict[str, Any]:
    """Normalize a GBIF match response into a small, UI-ready result.

    Returns keys:
      matched      - True unless matchType is NONE / missing
      canonical    - accepted canonical name ("Nerium oleander"), or ""
      scientific   - full scientific name with author, or ""
      rank         - SPECIES / GENUS / ...
      status       - ACCEPTED / SYNONYM / ...
      match_type   - EXACT / FUZZY / HIGHERRANK / NONE
      confidence   - int 0-100
      family       - botanical family, or ""
      exact        - True only on an EXACT species-rank accepted match
      alternatives - up to 5 other canonical names GBIF suggested
      note         - one-line human summary
    """
    if not isinstance(payload, dict):
        return {"matched": False, "match_type": "NONE", "note": "no response", "alternatives": []}
    mt = str(payload.get("matchType") or "NONE").upper()
    canonical = str(payload.get("canonicalName") or "")
    scientific = str(payload.get("scientificName") or "")
    rank = str(payload.get("rank") or "")
    status = str(payload.get("status") or "")
    confidence = payload.get("confidence")
    confidence = int(confidence) if isinstance(confidence, (int, float)) else 0
    family = str(payload.get("family") or "")

    alts: list[str] = []
    for a in payload.get("alternatives") or []:
        if not isinstance(a, dict):
            continue
        c = str(a.get("canonicalName") or "").strip()
        if c and c != canonical and c not in alts:
            alts.append(c)
        if len(alts) >= 5:
            break

    matched = mt != "NONE" and bool(canonical)
    exact = mt == "EXACT" and rank == "SPECIES" and status in ("ACCEPTED", "")

    if not matched:
        note = "No plant match found."
        if alts:
            note += " Did you mean: " + ", ".join(alts[:3]) + "?"
    elif mt == "FUZZY":
        note = f"Closest match: {canonical} (corrected a likely typo)."
    elif mt == "HIGHERRANK":
        note = f"Matched only to {rank.lower() or 'a higher rank'}: {canonical} — add the species."
    elif status == "SYNONYM":
        note = f"{canonical} is a synonym; accepted name may differ."
    else:
        note = f"Verified: {canonical}."

    return {
        "matched": matched,
        "canonical": canonical,
        "scientific": scientific,
        "rank": rank,
        "status": status,
        "match_type": mt,
        "confidence": confidence,
        "family": family,
        "exact": exact,
        "alternatives": alts,
        "note": note,
    }
