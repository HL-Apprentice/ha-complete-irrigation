"""Open-Meteo weather-source parsing (v1.49) — pure, real response shape."""

from __future__ import annotations

from custom_components.complete_irrigation.open_meteo import open_meteo_url, parse_open_meteo

# Real shape (verified against api.open-meteo.com 2026-07-16 for Gilbert AZ).
REAL = {
    "daily": {
        "time": [
            "2026-07-16",
            "2026-07-17",
            "2026-07-18",
            "2026-07-19",
            "2026-07-20",
            "2026-07-21",
            "2026-07-22",
        ],
        "et0_fao_evapotranspiration": [7.97, 2.95, 8.1, 8.0, 7.5, 7.2, 6.9],
        "temperature_2m_max": [40.4, 28.7, 41.0, 40.0, 39.0, 38.0, 37.0],
        "precipitation_sum": [0.0, 6.9, 0.0, 0.0, 0.0, 0.0, 0.0],
    }
}


def test_url_has_et0_and_no_key():
    u = open_meteo_url(33.31, -111.81)
    assert u.startswith("https://api.open-meteo.com/v1/forecast")
    assert "et0_fao_evapotranspiration" in u
    assert "latitude=33.31000" in u and "longitude=-111.81000" in u
    assert "timezone=auto" in u
    assert "key" not in u.lower()  # keyless


def test_weekly_eto_is_summed_mm_to_inches():
    r = parse_open_meteo(REAL)
    # sum = 48.62 mm -> / 25.4 = 1.914 in
    assert r["eto_week_in"] == 1.914
    assert r["days"] == 7


def test_forecast_high_is_max_of_next_two_days_in_f():
    r = parse_open_meteo(REAL)
    # max(40.4, 28.7) C = 40.4 C -> 104.72 F
    assert r["forecast_high_f"] == 104.7


def test_precip_summed_to_inches():
    r = parse_open_meteo(REAL)
    assert r["precip_week_in"] == round(6.9 / 25.4, 3)


def test_partial_and_null_values_are_tolerated():
    r = parse_open_meteo(
        {"daily": {"et0_fao_evapotranspiration": [5.0, None, 5.0], "temperature_2m_max": [None]}}
    )
    assert r["eto_week_in"] == round(10.0 / 25.4, 3)
    assert r["days"] == 2
    assert r["forecast_high_f"] is None


def test_garbage_is_safe():
    for bad in (None, {}, {"daily": "nope"}, {"daily": {}}, "x"):
        r = parse_open_meteo(bad)
        assert r["eto_week_in"] is None and r["days"] == 0
