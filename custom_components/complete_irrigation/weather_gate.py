"""WeatherGate — pure-logic weather-driven decisions.

  • evaluate_rain_lockout(rainfall_in, daily_eto) → ET-scaled lockout hours or None
  • evaluate_hot_weather(daily_high, threshold, boost%) → boost decision

The HA-coupled coordinator reads sensor values, calls these, and applies
the results (skip runs during lockout, scale up runtime when hot).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import pairwise


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


def temp_to_fahrenheit(value: float, unit: str | None) -> float | None:
    """Normalize a temperature reading to FAHRENHEIT regardless of the source unit.

    The rain-lockout ceiling anchors are in F. A weather entity or sensor may
    report Celsius (or, rarely, Kelvin); reading a C value AS F would wildly
    mis-scale the ceiling (30C read as 30F). Celsius/Kelvin are converted;
    Fahrenheit / unknown / blank is assumed already F (the US default for this
    desert integration). Non-numeric or non-finite -> None.
    """
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    u = (unit or "").strip().lower().lstrip("°")
    if u in ("c", "celsius"):
        return v * 9.0 / 5.0 + 32.0
    if u in ("k", "kelvin"):
        return (v - 273.15) * 9.0 / 5.0 + 32.0
    return v  # fahrenheit / unknown -> already F


# 4-model agronomy panel consensus (Grok+Gemini+Qwen+Claude, 2026-07): an
# ET-SCALED formula beats fixed tiers — lockout duration = how many days of
# evaporative demand the EFFECTIVE rainfall replaces, scaled by the LIVE ETo the
# integration already computes (FAO-56). Flexible + seasonal by construction.
RAIN_INTERCEPTION_LOSS_IN = 0.10  # first 0.1" lost to canopy interception + surface evap (arid)
RAIN_LOCKOUT_MIN_INCHES = 0.10  # default floor: below this, rain never locks out (trace/monsoon)
RAIN_LOCKOUT_MIN_HOURS = 3  # if it locks out at all, at least this long (avoid sub-noise)
RAIN_LOCKOUT_MAX_HOURS = 48  # mild-day ceiling (temp <= 85F): err TOWARD watering
RAIN_LOCKOUT_UNKNOWN_TEMP_HOURS = 24  # temp signal missing -> FAIL-SAFE: assume it may be a
# hot day and never hold water >1 day (a failed sensor in a heatwave must not default to the
# LONGEST lockout; matches Peter's rule "no lockout >1 day when it could be hot")
RAIN_LOCKOUT_FALLBACK_DAILY_ETO_IN = 0.25  # when live ETo is unavailable — biases short (summer)

# Graduated lockout ceiling by the day's heat (hottest of current + forecast high).
# Hotter -> plants transpire faster AND baked/hydrophobic desert soil sheds more of
# the rain, so a big event must never strand them: the max shrinks with temperature.
# Piecewise-linear through these anchors (flat outside the endpoints):
#   <=85F -> 48h (2 days, mild)   95F -> 36h   105F -> 24h (1 day)   >=115F -> 18h (extreme)
RAIN_LOCKOUT_MAX_ANCHORS = ((85.0, 48), (95.0, 36), (105.0, 24), (115.0, 18))


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


def rain_lockout_max_hours(temp_f: float | None) -> int:
    """Graduated lockout ceiling (hours) for the day's heat.

    `temp_f` is the hottest relevant temperature — the max of the current temp
    and the forecast high — so the ceiling reflects the heat the plants will
    actually face during the lockout. Hotter -> shorter ceiling, so a big rain
    never strands the yard in the heat. Piecewise-linear through
    RAIN_LOCKOUT_MAX_ANCHORS; flat outside the endpoints. Missing/NaN temp ->
    a fail-safe 24h ceiling (assume it MAY be hot; never default a blind sensor
    to the longest lockout — err toward watering per the prime directive).
    """
    if temp_f is None or not math.isfinite(temp_f):
        return RAIN_LOCKOUT_UNKNOWN_TEMP_HOURS
    anchors = RAIN_LOCKOUT_MAX_ANCHORS
    if temp_f <= anchors[0][0]:
        return anchors[0][1]
    if temp_f >= anchors[-1][0]:
        return anchors[-1][1]
    for (t0, h0), (t1, h1) in pairwise(anchors):
        if t0 <= temp_f <= t1:
            frac = (temp_f - t0) / (t1 - t0)
            return round(h0 + frac * (h1 - h0))
    return RAIN_LOCKOUT_MAX_HOURS  # unreachable (anchors cover the range)


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
