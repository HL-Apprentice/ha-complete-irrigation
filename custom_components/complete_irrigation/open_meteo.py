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

from datetime import date, datetime
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


def open_meteo_rain_url(lat: float, lon: float, tz: str | None = None) -> str:
    """72h history + next-day forecast for the rain lockout.

    Separate from ``open_meteo_url`` so the ETo fetch stays a 7-day forecast.
    ``past_days=3`` is what Open-Meteo needs to cover the trailing 72 hours.
    Hourly precipitation is the 72h sum and the next-24h total; daily ET0 is
    the forecast evaporative demand used to scale the lockout. Metric units
    (mm, °C) - the parser converts.
    """
    timezone = quote(tz) if tz else "auto"
    return (
        f"{_FORECAST}?latitude={float(lat):.5f}&longitude={float(lon):.5f}"
        "&daily=precipitation_sum,precipitation_probability_max,"
        "et0_fao_evapotranspiration,temperature_2m_max"
        "&hourly=precipitation,precipitation_probability"
        f"&past_days=3&forecast_days=2&timezone={timezone}"
    )


def _iso_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed


def _as_naive(moment: datetime) -> datetime:
    return moment.replace(tzinfo=None) if moment.tzinfo else moment


def parse_open_meteo_rain(payload: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Pull the lockout's rain figures out of an Open-Meteo forecast payload.

    ``past_72h_in`` sums completed hours only: hour-start in
    ``(-72h, -1h]``. The in-progress hour is excluded (Open-Meteo revises it,
    and it is still falling). The hour that starts exactly 72h ago is outside
    the window, matching the Tempest statistic window. ``forecast_24h_in``
    sums ``(now, now+24h]``, and
    ``forecast_24h_probability_pct`` is the max hourly probability in that
    window (Open-Meteo reports percent, 0-100). ``forecast_daily_eto_in`` is
    the hotter of today and tomorrow's FAO ET0, in inches. When hourly data
    is missing, the past 72h falls back to the daily sums for today and the
    previous two days.

    All rain totals are inches. Missing sections stay None - the caller falls
    back to the other source.
    """
    out: dict[str, Any] = {
        "past_72h_in": None,
        "forecast_24h_in": None,
        "forecast_24h_probability_pct": None,
        "forecast_daily_eto_in": None,
        "current_precip_mm": None,
    }
    if not isinstance(payload, dict):
        return out
    now_cmp = _as_naive(now) if isinstance(now, datetime) else None

    hourly = payload.get("hourly") if isinstance(payload.get("hourly"), dict) else {}
    times = hourly.get("time") if isinstance(hourly.get("time"), list) else []
    precip = hourly.get("precipitation") if isinstance(hourly.get("precipitation"), list) else []
    probs = (
        hourly.get("precipitation_probability")
        if isinstance(hourly.get("precipitation_probability"), list)
        else []
    )
    if now_cmp is not None and times and precip:
        past = 0.0
        future = 0.0
        past_n = 0
        future_n = 0
        max_prob: float | None = None
        current_mm: float | None = None
        for index, raw_time in enumerate(times):
            stamp = _iso_dt(raw_time)
            if stamp is None or index >= len(precip):
                continue
            millimeters = precip[index]
            if not isinstance(millimeters, (int, float)):
                continue
            delta_h = (_as_naive(stamp) - now_cmp).total_seconds() / 3600.0
            if -72.0 < delta_h <= -1.0:
                past += float(millimeters)
                past_n += 1
            elif -1.0 < delta_h <= 0.0:
                # In-progress hour. Used for "raining now", not the 72h total.
                current_mm = float(millimeters)
            elif 0.0 < delta_h <= 24.0:
                future += float(millimeters)
                future_n += 1
                if index < len(probs) and isinstance(probs[index], (int, float)):
                    probability = float(probs[index])
                    max_prob = probability if max_prob is None else max(max_prob, probability)
        if past_n:
            out["past_72h_in"] = round(past / _MM_PER_INCH, 4)
        if future_n:
            out["forecast_24h_in"] = round(future / _MM_PER_INCH, 4)
            out["forecast_24h_probability_pct"] = max_prob
        if current_mm is not None:
            out["current_precip_mm"] = current_mm

    daily = payload.get("daily") if isinstance(payload.get("daily"), dict) else {}
    day_times = daily.get("time") if isinstance(daily.get("time"), list) else []
    day_sums = (
        daily.get("precipitation_sum") if isinstance(daily.get("precipitation_sum"), list) else []
    )
    if out["past_72h_in"] is None and now_cmp is not None and day_times and day_sums:
        today = now_cmp.date()
        total = 0.0
        count = 0
        for index, raw_day in enumerate(day_times):
            if not isinstance(raw_day, str) or index >= len(day_sums):
                continue
            if not isinstance(day_sums[index], (int, float)):
                continue
            try:
                day = date.fromisoformat(raw_day[:10])
            except ValueError:
                continue
            age = (today - day).days
            if 0 <= age <= 2:
                total += float(day_sums[index])
                count += 1
        if count:
            out["past_72h_in"] = round(total / _MM_PER_INCH, 4)

    et0 = (
        daily.get("et0_fao_evapotranspiration")
        if isinstance(daily.get("et0_fao_evapotranspiration"), list)
        else []
    )
    eto_vals: list[float] = []
    if now_cmp is not None and et0 and day_times:
        today = now_cmp.date()
        for index, raw_day in enumerate(day_times):
            if index >= len(et0) or not isinstance(et0[index], (int, float)):
                continue
            if not isinstance(raw_day, str):
                continue
            try:
                day = date.fromisoformat(raw_day[:10])
            except ValueError:
                continue
            if 0 <= (day - today).days <= 1:
                eto_vals.append(float(et0[index]) / _MM_PER_INCH)
    elif et0:
        eto_vals = [
            float(value) / _MM_PER_INCH for value in et0[:2] if isinstance(value, (int, float))
        ]
    if eto_vals:
        out["forecast_daily_eto_in"] = round(max(eto_vals), 4)

    if out["forecast_24h_in"] is None and day_sums:
        # Coarse stand-in: the first daily bucket at or after today.
        if now_cmp is not None and day_times:
            today = now_cmp.date()
            chosen: float | None = None
            for index, raw_day in enumerate(day_times):
                if not isinstance(raw_day, str) or index >= len(day_sums):
                    continue
                if not isinstance(day_sums[index], (int, float)):
                    continue
                try:
                    day = date.fromisoformat(raw_day[:10])
                except ValueError:
                    continue
                if 0 <= (day - today).days <= 1:
                    chosen = float(day_sums[index])
                    break
            if chosen is not None:
                out["forecast_24h_in"] = round(chosen / _MM_PER_INCH, 4)
        elif isinstance(day_sums[0], (int, float)):
            out["forecast_24h_in"] = round(float(day_sums[0]) / _MM_PER_INCH, 4)
    if out["forecast_24h_probability_pct"] is None:
        day_probs = daily.get("precipitation_probability_max")
        if isinstance(day_probs, list) and now_cmp is not None and day_times:
            today = now_cmp.date()
            numbers: list[float] = []
            for index, raw_day in enumerate(day_times):
                if index >= len(day_probs) or not isinstance(day_probs[index], (int, float)):
                    continue
                if not isinstance(raw_day, str):
                    continue
                try:
                    day = date.fromisoformat(raw_day[:10])
                except ValueError:
                    continue
                if 0 <= (day - today).days <= 1:
                    numbers.append(float(day_probs[index]))
            if numbers:
                out["forecast_24h_probability_pct"] = max(numbers)
        elif isinstance(day_probs, list):
            numbers = [float(value) for value in day_probs[:2] if isinstance(value, (int, float))]
            if numbers:
                out["forecast_24h_probability_pct"] = max(numbers)
    return out
