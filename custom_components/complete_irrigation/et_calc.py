"""Reference evapotranspiration (ETo) from weather — FAO-56 Penman-Monteith.

Pure logic, HA-free. Wraps the vendored PyETO (BSD-3-Clause, (c) 2015 Mark
Richards; see pyeto/LICENSE.txt) to compute daily ETo from the weather a HA
weather entity / sensors provide, then a weekly total in INCHES to feed the
existing plant water-need math (need = ETo_in x plant_factor x area x 0.623 /
drip_efficiency) — replacing the MANUAL `eto_in_week`.

Graceful degradation (the way Smart Irrigation does it): full FAO-56 Penman-
Monteith when humidity + wind are available; temperature-only **Hargreaves** when
they aren't. Solar radiation is used if measured, else estimated from the daily
temperature range. So it produces a sane ETo even on sparse sensors.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import pyeto

MM_PER_INCH = 25.4


@dataclass(frozen=True)
class DayWeather:
    """One day's weather. tmin/tmax are required; everything else is optional and
    drives whether we can run full Penman-Monteith or fall back to Hargreaves."""

    tmin_c: float
    tmax_c: float
    wind_m_s: float | None = None
    rh_min: float | None = None
    rh_max: float | None = None
    rh_mean: float | None = None
    dewpoint_c: float | None = None
    pressure_kpa: float | None = None
    solar_rad_mj: float | None = None  # measured MJ m-2 day-1, if available


def _actual_vapour_pressure(day: DayWeather) -> float | None:
    """Actual vapour pressure [kPa] from the best humidity source available, or
    None if there is no humidity data (-> Hargreaves)."""
    svp_min = pyeto.svp_from_t(day.tmin_c)
    svp_max = pyeto.svp_from_t(day.tmax_c)
    if day.dewpoint_c is not None:
        return pyeto.avp_from_tdew(day.dewpoint_c)
    if day.rh_min is not None and day.rh_max is not None:
        return pyeto.avp_from_rhmin_rhmax(svp_min, svp_max, day.rh_min, day.rh_max)
    if day.rh_max is not None:
        return pyeto.avp_from_rhmax(svp_min, day.rh_max)
    if day.rh_mean is not None:
        return pyeto.avp_from_rhmean(svp_min, svp_max, day.rh_mean)
    return None


def daily_eto_mm(
    day: DayWeather,
    *,
    latitude_deg: float,
    altitude_m: float,
    day_of_year: int,
    coastal: bool = False,
) -> float:
    """Reference ETo [mm] for one day. Full FAO-56 Penman-Monteith when humidity +
    wind are present; otherwise temperature-only Hargreaves. Never negative."""
    lat = pyeto.deg2rad(latitude_deg)
    sd = pyeto.sol_dec(day_of_year)
    sha = pyeto.sunset_hour_angle(lat, sd)
    ird = pyeto.inv_rel_dist_earth_sun(day_of_year)
    et_rad = pyeto.et_rad(lat, sd, sha, ird)
    cs_rad = pyeto.cs_rad(altitude_m, et_rad)
    tmean = (day.tmin_c + day.tmax_c) / 2

    avp = _actual_vapour_pressure(day)
    if avp is None or day.wind_m_s is None:
        # Not enough for Penman-Monteith -> Hargreaves (temperature only).
        return max(0.0, pyeto.hargreaves(day.tmin_c, day.tmax_c, tmean, et_rad))

    sol_rad = day.solar_rad_mj
    if sol_rad is None:
        sol_rad = pyeto.sol_rad_from_t(et_rad, cs_rad, day.tmin_c, day.tmax_c, coastal)
    svp = pyeto.mean_svp(day.tmin_c, day.tmax_c)
    delta = pyeto.delta_svp(tmean)
    pressure = day.pressure_kpa if day.pressure_kpa else pyeto.atm_pressure(altitude_m)
    psy = pyeto.psy_const(pressure)
    ni_sw = pyeto.net_in_sol_rad(sol_rad)
    no_lw = pyeto.net_out_lw_rad(
        pyeto.celsius2kelvin(day.tmin_c), pyeto.celsius2kelvin(day.tmax_c), sol_rad, cs_rad, avp
    )
    eto = pyeto.fao56_penman_monteith(
        pyeto.net_rad(ni_sw, no_lw),
        pyeto.celsius2kelvin(tmean),
        day.wind_m_s,
        svp,
        avp,
        delta,
        psy,
    )
    return max(0.0, eto)


def weekly_eto_inches(
    days: list[DayWeather],
    *,
    latitude_deg: float,
    altitude_m: float,
    start_day_of_year: int,
    coastal: bool = False,
) -> float:
    """Total weekly ETo in INCHES from a list of consecutive days (a weather
    forecast). Normalizes the daily mean up to a full 7-day week so a 5-day
    forecast still yields a weekly figure comparable to the manual eto_in_week."""
    if not days:
        return 0.0
    total_mm = sum(
        daily_eto_mm(
            d,
            latitude_deg=latitude_deg,
            altitude_m=altitude_m,
            day_of_year=start_day_of_year + i,
            coastal=coastal,
        )
        for i, d in enumerate(days)
    )
    mean_mm = total_mm / len(days)
    return (mean_mm * 7) / MM_PER_INCH


# ── HA weather-forecast adaptation (unit conversion -> DayWeather) ────
# Kept here (pure) so the finicky unit handling is unit-tested without HA. The
# coordinator does the async forecast fetch + reads the entity's unit attributes,
# then hands the raw forecast list to weekly_eto_inches_from_forecast().

_WIND_TO_M_S = {
    "m/s": 1.0,
    "km/h": 1.0 / 3.6,
    "kmh": 1.0 / 3.6,
    "mph": 0.44704,
    "mi/h": 0.44704,
    "kn": 0.514444,
    "kt": 0.514444,
    "knot": 0.514444,
    "ft/s": 0.3048,
}


def _to_celsius(value: float, unit: str | None) -> float:
    u = str(unit or "").strip().lower().replace("°", "")
    if u in ("f", "fahrenheit"):
        return (float(value) - 32.0) * 5.0 / 9.0
    return float(value)  # assume Celsius (HA's default and FAO unit)


def _to_m_s(value: float, unit: str | None) -> float:
    u = str(unit or "").strip().lower()
    return float(value) * _WIND_TO_M_S.get(u, 1.0)  # unknown -> assume m/s


def day_from_forecast(
    entry: dict, *, temp_unit: str | None, wind_unit: str | None
) -> DayWeather | None:
    """Map one HA daily-forecast dict to a DayWeather (units -> C / m/s). Needs
    both `temperature` (high) and `templow` (low); returns None otherwise (so that
    day is skipped). A daily forecast carries one `humidity` -> rh_mean."""
    tmax, tmin = entry.get("temperature"), entry.get("templow")
    if tmax is None or tmin is None:
        return None
    wind = entry.get("wind_speed")
    rh = entry.get("humidity")
    return DayWeather(
        tmin_c=_to_celsius(tmin, temp_unit),
        tmax_c=_to_celsius(tmax, temp_unit),
        wind_m_s=_to_m_s(wind, wind_unit) if wind is not None else None,
        rh_mean=float(rh) if rh is not None else None,
    )


def weekly_eto_inches_from_forecast(
    forecast: list[dict],
    *,
    latitude_deg: float,
    altitude_m: float,
    temp_unit: str | None,
    wind_unit: str | None,
    start_day_of_year: int,
    coastal: bool = False,
) -> float:
    """Weekly ETo [inches] from a HA daily forecast (the coordinator's entry point).
    Returns 0.0 if no day in the forecast has both a high and a low."""
    days = [
        d
        for d in (day_from_forecast(e, temp_unit=temp_unit, wind_unit=wind_unit) for e in forecast)
        if d is not None
    ]
    return weekly_eto_inches(
        days,
        latitude_deg=latitude_deg,
        altitude_m=altitude_m,
        start_day_of_year=start_day_of_year,
        coastal=coastal,
    )


# ── Source selection (auto vs manual) — pure policy, HA-free ──────────

DEFAULT_AUTO_STALE_HOURS = 36.0


def select_eto(
    *,
    manual: float | None,
    auto_value: float | None,
    auto_enabled: bool,
    auto_age_hours: float | None,
    default: float,
    stale_hours: float = DEFAULT_AUTO_STALE_HOURS,
) -> tuple[float, str]:
    """Pick the reference ETo (in/week) the design math should use, with graceful
    fallback. Returns (value, source) where source is 'auto' or 'manual'.

    Auto wins ONLY when it's enabled, positive, and fresh (age <= stale_hours, so a
    dark weather feed can't strand us on a days-old number). Otherwise the manual
    value — or `default` when manual is missing/invalid/non-positive. Never raises,
    always returns a positive value, so callers can trust it for water math."""
    try:
        m = float(manual) if manual is not None else default
    except (TypeError, ValueError):
        m = default
    if m <= 0:
        m = default
    if not auto_enabled:
        return m, "manual"
    if (
        auto_value is not None
        and auto_value > 0
        and auto_age_hours is not None
        and auto_age_hours <= stale_hours
    ):
        return float(auto_value), "auto"
    return m, "manual"
