"""Open-Meteo weather source — pure logic, HA-free.

Open-Meteo (open-meteo.com) is a free, **keyless** forecast API. Crucially for
this integration it returns **FAO reference evapotranspiration
(`et0_fao_evapotranspiration`, mm/day)** directly — exactly the quantity the
water calculator needs — so a user with no Home Assistant weather entity (or who
just doesn't want to wire one up) can still get automatic ETo. It also gives the
daily high (for the hot-weather / rain-lockout heat signal).

This module builds the request URL and parses the response into the same figures
the coordinator already uses (weekly ETo in inches, near-term high in °F). The
thin async fetch lives in the coordinator, like the other outbound calls.

No API key. Non-commercial use is free (this is a non-commercial hobbyist tool).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

_FORECAST = "https://api.open-meteo.com/v1/forecast"
_MM_PER_INCH = 25.4


def open_meteo_url(lat: float, lon: float, tz: str | None = None) -> str:
    """Daily-forecast URL for (lat, lon): FAO ET0 + high + precip, 7 days.

    Metric units (mm, °C) — the parser converts. `tz` sets the day boundaries;
    "auto" (the default) uses the location's local timezone so daily totals line
    up with the yard's day.
    """
    timezone = quote(tz) if tz else "auto"
    return (
        f"{_FORECAST}?latitude={float(lat):.5f}&longitude={float(lon):.5f}"
        "&daily=et0_fao_evapotranspiration,temperature_2m_max,precipitation_sum"
        f"&forecast_days=7&timezone={timezone}"
    )


def _c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def parse_open_meteo(payload: Any) -> dict[str, Any]:
    """Normalize an Open-Meteo daily response.

    Returns:
      eto_week_in     - sum of the next 7 days' FAO ET0, in inches (None if none)
      forecast_high_f - max daily high of the next 2 days, °F (None if none)
      precip_week_in  - next 7 days' precipitation total, inches (None if none)
      days            - how many ET0 days were usable
    """
    out = {"eto_week_in": None, "forecast_high_f": None, "precip_week_in": None, "days": 0}
    if not isinstance(payload, dict):
        return out
    daily = payload.get("daily")
    if not isinstance(daily, dict):
        return out

    def _nums(key: str) -> list[float]:
        vals = daily.get(key)
        if not isinstance(vals, list):
            return []
        return [float(v) for v in vals if isinstance(v, (int, float))]

    et0 = _nums("et0_fao_evapotranspiration")[:7]
    if et0:
        out["eto_week_in"] = round(sum(et0) / _MM_PER_INCH, 3)
        out["days"] = len(et0)

    highs = _nums("temperature_2m_max")[:2]
    if highs:
        out["forecast_high_f"] = round(_c_to_f(max(highs)), 1)

    precip = _nums("precipitation_sum")[:7]
    if precip:
        out["precip_week_in"] = round(sum(precip) / _MM_PER_INCH, 3)

    return out
