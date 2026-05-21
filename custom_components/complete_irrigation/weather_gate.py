"""WeatherGate — pure-logic weather-driven decisions.

  • evaluate_rain_lockout(rainfall_6h, tiers) → lockout hours or None
  • evaluate_hot_weather(daily_high, threshold, boost%) → boost decision

The HA-coupled coordinator reads sensor values, calls these, and applies
the results (skip runs during lockout, scale up runtime when hot).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .const import DEFAULT_RAIN_LOCKOUT_TIERS


def evaluate_rain_lockout(
    rainfall_inches_6h: float,
    tiers: Sequence[tuple[float, int]] | None = None,
) -> int | None:
    """Return the lockout duration in hours, or None if no lockout needed.

    `tiers` is a list of (rainfall_threshold_inches, lockout_hours)
    pairs. The highest threshold the rainfall meets wins.
    """
    table = tiers if tiers is not None else DEFAULT_RAIN_LOCKOUT_TIERS
    matched: int | None = None
    for threshold, hours in sorted(table, key=lambda x: x[0]):
        if rainfall_inches_6h >= threshold:
            matched = hours
    return matched


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
    if boost_percent <= 0 or daily_high_f < threshold_f:
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
