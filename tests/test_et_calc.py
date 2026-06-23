"""v1.28 — FAO-56 ETo from weather (et_calc, wrapping vendored PyETO).

Pure logic, HA-free. Reference values cross-checked against the FAO-56 Penman-
Monteith / Hargreaves equations for a Phoenix-summer day (lat 33.4, ~337 m,
late June): PM ETo ~8.56 mm/day (~0.34 in/day), Hargreaves ~8.22 mm/day — both in
the textbook 0.30-0.45 in/day range and within ~5% of each other.
"""

from __future__ import annotations

from custom_components.complete_irrigation.et_calc import (
    MM_PER_INCH,
    DayWeather,
    daily_eto_mm,
    weekly_eto_inches,
)

PHX = {"latitude_deg": 33.4, "altitude_m": 337, "day_of_year": 180}


def _phx_day(**kw) -> DayWeather:
    base = {"tmin_c": 25.0, "tmax_c": 42.0, "wind_m_s": 2.0, "rh_min": 10.0, "rh_max": 40.0}
    base.update(kw)
    return DayWeather(**base)


def test_penman_monteith_phoenix_summer():
    eto = daily_eto_mm(_phx_day(), **PHX)
    assert 8.0 < eto < 9.5, eto  # ~8.56 mm/day
    assert 0.30 < eto / MM_PER_INCH < 0.40  # in/day, textbook Phoenix-summer range


def test_hargreaves_fallback_when_no_wind_or_humidity():
    eto = daily_eto_mm(DayWeather(tmin_c=25.0, tmax_c=42.0), **PHX)
    assert 7.5 < eto < 9.0, eto  # ~8.22 mm/day (temperature-only)


def test_pm_and_hargreaves_agree_within_25pct():
    pm = daily_eto_mm(_phx_day(), **PHX)
    hg = daily_eto_mm(DayWeather(tmin_c=25.0, tmax_c=42.0), **PHX)
    assert abs(pm - hg) / pm < 0.25


def test_dewpoint_runs_penman_monteith():
    eto = daily_eto_mm(DayWeather(tmin_c=25.0, tmax_c=42.0, wind_m_s=2.0, dewpoint_c=10.0), **PHX)
    assert 7.0 < eto < 11.0, eto


def test_cold_humid_calm_day_low_eto():
    eto = daily_eto_mm(
        DayWeather(tmin_c=2.0, tmax_c=10.0, wind_m_s=1.0, rh_min=70.0, rh_max=95.0),
        latitude_deg=33.4,
        altitude_m=337,
        day_of_year=15,
    )
    assert 0.0 <= eto < 2.0, eto


def test_never_negative_in_extreme_cold():
    eto = daily_eto_mm(
        DayWeather(tmin_c=-5.0, tmax_c=-1.0, wind_m_s=1.0, rh_min=80.0, rh_max=99.0),
        latitude_deg=60.0,
        altitude_m=0,
        day_of_year=355,
    )
    assert eto >= 0.0


def test_weekly_inches_normalizes_a_short_forecast_to_seven_days():
    days = [_phx_day() for _ in range(5)]  # 5-day forecast
    wk = weekly_eto_inches(days, latitude_deg=33.4, altitude_m=337, start_day_of_year=180)
    assert 2.0 < wk < 2.7, wk  # ~8.5 mm/day * 7 / 25.4 ~= 2.35 in/week


def test_empty_forecast_is_zero():
    assert weekly_eto_inches([], latitude_deg=33.4, altitude_m=337, start_day_of_year=180) == 0.0


def test_hotter_day_means_more_eto():
    cooler = daily_eto_mm(_phx_day(tmax_c=35.0), **PHX)
    hotter = daily_eto_mm(_phx_day(tmax_c=45.0), **PHX)
    assert hotter > cooler
