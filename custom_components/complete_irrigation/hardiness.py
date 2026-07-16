"""USDA plant-hardiness zone lookup — pure logic, HA-free.

phzmapi.org is a free, **keyless** API that maps a US ZIP code to its USDA
plant-hardiness zone (e.g. "9b") — the standard measure of a location's average
annual coldest temperature. We use it to give a frost-planning temperature for
the yard, and to flag plants whose cold tolerance is warmer than the zone gets
(so they may need winter protection).

This module builds the request URL and parses the response, including deriving
the zone's temperature band from its label. The thin async fetch lives in
services.py.
"""

from __future__ import annotations

import re
from typing import Any

_PHZM = "https://phzmapi.org"
_ZONE_RE = re.compile(r"^\s*(\d{1,2})\s*([ab])\s*$", re.IGNORECASE)


def phzm_url(zip_code: str) -> str:
    """phzmapi URL for a 5-digit US ZIP (digits only)."""
    z = re.sub(r"\D", "", str(zip_code or ""))[:5]
    return f"{_PHZM}/{z}.json"


def zone_temp_band_f(zone: str) -> tuple[int, int] | None:
    """(low, high) °F band for a USDA zone label like "9b".

    USDA bands are 10 °F wide per number, split into a/b halves of 5 °F, starting
    at zone 1a = [-60, -55]. So 9b -> base -60 + (9-1)*10 = 20; the "b" half is
    [25, 30]. The LOW end is the frost-planning number.
    """
    m = _ZONE_RE.match(str(zone or ""))
    if not m:
        return None
    n = int(m.group(1))
    if not (1 <= n <= 13):
        return None
    base = -60 + (n - 1) * 10
    low = base + (0 if m.group(2).lower() == "a" else 5)
    return (low, low + 5)


def parse_phzm(payload: Any) -> dict[str, Any]:
    """Normalize a phzmapi response.

    Returns:
      zone         - USDA zone label ("9b"), or ""
      temp_low_f   - coldest expected temp for the zone, °F (None if unknown)
      temp_high_f  - warm end of the zone's band, °F (None if unknown)
      matched      - True when a usable zone was found
    """
    out = {"zone": "", "temp_low_f": None, "temp_high_f": None, "matched": False}
    if not isinstance(payload, dict):
        return out
    zone = str(payload.get("zone") or "").strip()
    band = zone_temp_band_f(zone)
    if not zone or band is None:
        return out
    out["zone"] = zone
    out["temp_low_f"], out["temp_high_f"] = band
    out["matched"] = True
    return out


def frost_tender(plant_temp_low_f: Any, zone_temp_low_f: Any) -> bool:
    """True when a plant's cold tolerance is WARMER than the zone's coldest temp —
    i.e. the zone can get colder than the plant survives, so it may need winter
    protection. Both °F. False on missing data (never a false alarm)."""
    try:
        return float(plant_temp_low_f) > float(zone_temp_low_f)
    except (TypeError, ValueError):
        return False
