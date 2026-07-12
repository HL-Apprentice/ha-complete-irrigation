"""WeatherGate — pure-logic weather-driven decisions.

  • evaluate_rain_lockout(rainfall_in, daily_eto) → ET-scaled lockout hours or None
  • evaluate_hot_weather(daily_high, threshold, boost%) → boost decision

The HA-coupled coordinator reads sensor values, calls these, and applies
the results (skip runs during lockout, scale up runtime when hot).
"""

from __future__ import annotations

import math
from dataclasses import dataclass


def rain_to_inches(value: float, unit: str | None) -> float | None:
    """Normalize a rain-sensor reading to INCHES regardless of the sensor's unit.

    Tempest/WeatherFlow and many rain sensors report millimetres; the lockout
    tiers are in inches. Reading a mm value AS inches is the classic bug that
    makes a 0.25 mm (0.01") sprinkle look like 0.25" and trip a lockout. This
    converts explicitly. Unknown/blank unit is assumed to be inches (the config
    default), non-finite -> None.
    """
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    u = (unit or "").strip().lower()
    if u in ("mm", "millimeter", "millimeters", "millimetre", "millimetres"):
        return v / 25.4
    if u in ("cm", "centimeter", "centimeters"):
        return v / 2.54
    if u in ("m", "meter", "meters", "metre", "metres"):
        return v * 39.3701
    # inches ("in", "inch", "inches", '"') or unknown -> treat as inches
    return v


# 4-model agronomy panel consensus (Grok+Gemini+Qwen+Claude, 2026-07): an
# ET-SCALED formula beats fixed tiers — lockout duration = how many days of
# evaporative demand the EFFECTIVE rainfall replaces, scaled by the LIVE ETo the
# integration already computes (FAO-56). Flexible + seasonal by construction.
RAIN_INTERCEPTION_LOSS_IN = 0.10  # first 0.1" lost to canopy interception + surface evap (arid)
RAIN_LOCKOUT_MIN_INCHES = 0.10  # default floor: below this, rain never locks out (trace/monsoon)
RAIN_LOCKOUT_MIN_HOURS = 3  # if it locks out at all, at least this long (avoid sub-noise)
RAIN_LOCKOUT_MAX_HOURS = 48  # normal cap: err TOWARD watering + runoff overstates depth
RAIN_LOCKOUT_HOT_MAX_HOURS = 24  # hot-day cap: never strand plants >1 day in extreme heat
RAIN_LOCKOUT_FALLBACK_DAILY_ETO_IN = 0.25  # when live ETo is unavailable — biases short (summer)


def evaluate_rain_lockout(
    rainfall_inches: float,
    daily_eto_in: float | None = None,
    *,
    min_inches: float = RAIN_LOCKOUT_MIN_INCHES,
    interception_in: float = RAIN_INTERCEPTION_LOSS_IN,
    min_hours: int = RAIN_LOCKOUT_MIN_HOURS,
    max_hours: int = RAIN_LOCKOUT_MAX_HOURS,
    fallback_daily_eto_in: float = RAIN_LOCKOUT_FALLBACK_DAILY_ETO_IN,
) -> int | None:
    """Lockout duration in hours (or None for no lockout), ET-scaled.

    effective = max(0, rainfall - interception); below `min_inches` (or zero
    effective) -> no lockout. Otherwise hours = clamp(round(effective /
    daily_eto * 24), min_hours, max_hours). `daily_eto_in` is the LIVE daily ETo
    (weekly / 7); a non-positive/None/NaN value falls back to a summer-ish
    default that biases the lockout SHORT (err toward watering in heat).
    """
    if not math.isfinite(rainfall_inches):
        return None  # a NaN/inf sensor glitch must not (wrongly) trigger/pin a lockout
    floor = min_inches if (math.isfinite(min_inches) and min_inches > 0) else 0.0
    if rainfall_inches < floor:
        return None  # below the floor — a passing sprinkle, keep watering
    effective = max(0.0, rainfall_inches - max(0.0, interception_in))
    if effective <= 0:
        return None  # all intercepted/evaporated — never reached the root zone
    eto = (
        daily_eto_in
        if (daily_eto_in and math.isfinite(daily_eto_in) and daily_eto_in > 0)
        else fallback_daily_eto_in
    )
    hours = round(effective / eto * 24)
    return max(min_hours, min(max_hours, hours))


@dataclass(frozen=True)
class HotWeatherDecision:
    boost: bool
    multiplier: float  # runtime multiplier; 1.0 = no change

    @classmethod
    def none(cls) -> HotWeatherDecision:
        return cls(boost=False, multiplier=1.0)


def evaluate_hot_weather(
    daily_high_f: float,
    threshold_f: float,
    boost_percent: int,
) -> HotWeatherDecision:
    """Single-step boost — when the daily high meets `threshold_f`, apply
    `boost_percent` (as a percent) to runtime. Below threshold or
    zero boost → no change."""
    if boost_percent <= 0 or not math.isfinite(daily_high_f) or daily_high_f < threshold_f:
        # non-finite guard: a NaN high is False for `< threshold` and would wrongly
        # apply the hot-weather boost (over-water); treat it as no-boost.
        return HotWeatherDecision.none()
    return HotWeatherDecision(boost=True, multiplier=1.0 + boost_percent / 100.0)


# PRD #52 — wind-based defer. Default threshold conservative; tuned for
# typical fixed-nozzle drift (mph). Adjustable via set_weather_config.
DEFAULT_WIND_DEFER_MPH = 15.0


def evaluate_wind_defer(wind_mph: float | None, threshold_mph: float) -> bool:
    """Should this run be deferred because of wind?

    Returns True when the current wind speed meets or exceeds the
    threshold. None/NaN wind values fall through as False (no wind data
    → don't gate runs on it; user opt-in via setting the threshold).
    """
    if wind_mph is None:
        return False
    try:
        w = float(wind_mph)
    except (TypeError, ValueError):
        return False
    if threshold_mph <= 0:
        return False
    return w >= threshold_mph
