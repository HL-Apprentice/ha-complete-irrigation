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
    _to_celsius,
    _to_m_s,
    daily_eto_mm,
    day_from_forecast,
    select_eto,
    weekly_eto_inches,
    weekly_eto_inches_from_forecast,
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


# ── forecast adaptation + unit conversion ────────────────────────────

FC_PHX = {"latitude_deg": 33.4, "altitude_m": 337, "start_day_of_year": 180}


def test_unit_conversions():
    assert abs(_to_celsius(212, "°F") - 100.0) < 1e-6
    assert abs(_to_celsius(25, "°C") - 25.0) < 1e-6
    assert abs(_to_m_s(3.6, "km/h") - 1.0) < 1e-6
    assert abs(_to_m_s(1.0, "mph") - 0.44704) < 1e-6
    assert abs(_to_m_s(1.0, "kn") - 0.514444) < 1e-6
    assert abs(_to_m_s(5.0, "m/s") - 5.0) < 1e-6


def test_day_from_forecast_converts_metric():
    d = day_from_forecast(
        {"temperature": 42.0, "templow": 25.0, "humidity": 25, "wind_speed": 7.2},
        temp_unit="°C",
        wind_unit="km/h",
    )
    assert d is not None
    assert abs(d.tmax_c - 42.0) < 1e-6 and abs(d.tmin_c - 25.0) < 1e-6
    assert abs(d.wind_m_s - 2.0) < 1e-6  # 7.2 km/h
    assert d.rh_mean == 25.0


def test_day_from_forecast_missing_low_is_none():
    assert day_from_forecast({"temperature": 42.0}, temp_unit="°C", wind_unit="m/s") is None


def test_weekly_from_forecast_metric_and_imperial_agree():
    # The same physical day expressed in metric vs imperial must give the same ETo.
    metric = [{"temperature": 42.0, "templow": 25.0, "humidity": 25, "wind_speed": 7.2}] * 5
    imperial = [{"temperature": 107.6, "templow": 77.0, "humidity": 25, "wind_speed": 4.4704}] * 5
    wk_m = weekly_eto_inches_from_forecast(metric, temp_unit="°C", wind_unit="km/h", **FC_PHX)
    wk_i = weekly_eto_inches_from_forecast(imperial, temp_unit="°F", wind_unit="mph", **FC_PHX)
    assert abs(wk_m - wk_i) < 0.01, (wk_m, wk_i)
    assert 2.0 < wk_m < 2.7


def test_weekly_from_forecast_empty_or_unusable_is_zero():
    assert weekly_eto_inches_from_forecast([], temp_unit="°C", wind_unit="m/s", **FC_PHX) == 0.0
    # No templow -> day skipped -> nothing usable -> 0 (coordinator falls back to manual).
    assert (
        weekly_eto_inches_from_forecast(
            [{"temperature": 42.0}], temp_unit="°C", wind_unit="m/s", **FC_PHX
        )
        == 0.0
    )


# ── source selection (auto vs manual fallback) ───────────────────────

DEF = 1.5  # the integration's DEFAULT_ETO_IN_WEEK


def test_select_eto_auto_off_uses_manual():
    assert select_eto(
        manual=2.0, auto_value=3.0, auto_enabled=False, auto_age_hours=1.0, default=DEF
    ) == (2.0, "manual")


def test_select_eto_auto_on_fresh_uses_auto():
    assert select_eto(
        manual=2.0, auto_value=3.1, auto_enabled=True, auto_age_hours=2.0, default=DEF
    ) == (3.1, "auto")


def test_select_eto_auto_on_but_stale_falls_back_to_manual():
    # 40h old > 36h staleness window -> don't water on a days-old number.
    assert select_eto(
        manual=2.0, auto_value=3.1, auto_enabled=True, auto_age_hours=40.0, default=DEF
    ) == (2.0, "manual")


def test_select_eto_auto_on_no_value_yet_falls_back():
    assert select_eto(
        manual=2.0, auto_value=None, auto_enabled=True, auto_age_hours=None, default=DEF
    ) == (2.0, "manual")


def test_select_eto_auto_zero_value_falls_back():
    assert select_eto(
        manual=2.0, auto_value=0.0, auto_enabled=True, auto_age_hours=1.0, default=DEF
    ) == (2.0, "manual")


def test_select_eto_manual_missing_or_invalid_uses_default():
    assert select_eto(
        manual=None, auto_value=None, auto_enabled=False, auto_age_hours=None, default=DEF
    ) == (DEF, "manual")
    assert select_eto(
        manual="bad", auto_value=None, auto_enabled=False, auto_age_hours=None, default=DEF
    ) == (DEF, "manual")
    # Non-positive manual is nonsensical for water math -> default.
    assert select_eto(
        manual=0.0, auto_value=None, auto_enabled=False, auto_age_hours=None, default=DEF
    ) == (DEF, "manual")


def test_select_eto_always_returns_positive():
    # Every degraded path still yields a usable positive number.
    for kw in (
        dict(manual=None, auto_value=None, auto_enabled=True, auto_age_hours=None),
        dict(manual=-1, auto_value=-2, auto_enabled=True, auto_age_hours=99),
        dict(manual=None, auto_value=None, auto_enabled=False, auto_age_hours=None),
    ):
        value, _ = select_eto(default=DEF, **kw)
        assert value > 0
