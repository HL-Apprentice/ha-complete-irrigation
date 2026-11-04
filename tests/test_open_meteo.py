"""Open-Meteo weather-source parsing (v1.49) — pure, real response shape."""

from __future__ import annotations

from datetime import datetime

from custom_components.complete_irrigation.open_meteo import (
    open_meteo_rain_url,
    open_meteo_url,
    parse_open_meteo,
    parse_open_meteo_rain,
)

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


def test_rain_url_asks_for_three_past_days_and_no_key():
    url = open_meteo_rain_url(33.31, -111.81, "America/Phoenix")
    assert url.startswith("https://api.open-meteo.com/v1/forecast")
    assert "past_days=3" in url
    assert "hourly=precipitation,precipitation_probability" in url
    assert "precipitation_sum" in url
    assert "et0_fao_evapotranspiration" in url
    assert "timezone=America" in url
    assert "key" not in url.lower()


def test_rain_parser_sums_72h_and_the_next_24h():
    now = datetime(2026, 11, 4, 12, 0)
    parsed = parse_open_meteo_rain(
        {
            "hourly": {
                "time": [
                    "2026-11-04T10:00",
                    "2026-11-04T11:00",
                    "2026-11-04T12:00",
                    "2026-11-04T13:00",
                    "2026-11-05T13:00",  # 25h ahead - outside the 24h window
                ],
                "precipitation": [1.0, 2.0, 3.0, 25.4, 50.0],
                "precipitation_probability": [10, 20, 30, 80, 90],
            }
        },
        now=now,
    )
    # 10:00 and 11:00 are completed hours. 12:00 is the in-progress hour
    # and is not part of the 72h total. 3 mm.
    assert parsed["past_72h_in"] == round(3.0 / 25.4, 4)
    # Only 13:00 is inside the next 24h: 25.4 mm = 1 in, probability 80.
    assert parsed["forecast_24h_in"] == 1.0
    assert parsed["forecast_24h_probability_pct"] == 80
    assert parsed["current_precip_mm"] == 3.0


def test_rain_parser_drops_the_current_hour_and_the_73rd_bucket():
    now = datetime(2026, 11, 4, 12, 0)
    parsed = parse_open_meteo_rain(
        {
            "hourly": {
                "time": [
                    "2026-11-01T12:00",  # exactly -72h, the extra bucket
                    "2026-11-01T13:00",  # -71h, inside
                    "2026-11-04T11:00",  # last completed hour
                    "2026-11-04T12:00",  # in progress
                ],
                "precipitation": [100.0, 25.4, 25.4, 50.0],
            }
        },
        now=now,
    )
    assert parsed["past_72h_in"] == round(50.8 / 25.4, 4)
    assert parsed["current_precip_mm"] == 50.0


def test_rain_parser_falls_back_to_daily_sums():
    now = datetime(2026, 11, 4, 18, 0)
    parsed = parse_open_meteo_rain(
        {
            "daily": {
                "time": ["2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"],
                "precipitation_sum": [1.0, 2.0, 25.4, 25.4, 0.0],
                "precipitation_probability_max": [0, 10, 20, 70, 5],
                "et0_fao_evapotranspiration": [1.0, 2.0, 3.0, 5.08, 7.62],
            }
        },
        now=now,
    )
    # Today + previous 2 days (Nov 2 is 2 days before Nov 4? Nov 4-2=Nov 2).
    # age 0, 1, 2 -> Nov 4, Nov 3, Nov 2 = 25.4+25.4+2.0 mm. Nov 1 is age 3, excluded.
    assert parsed["past_72h_in"] == round((25.4 + 25.4 + 2.0) / 25.4, 4)
    assert parsed["forecast_24h_in"] == 1.0  # today's daily bucket, first in-window
    assert parsed["forecast_24h_probability_pct"] == 70
    # max(today 5.08, tomorrow 7.62) mm -> 0.3 in
    assert parsed["forecast_daily_eto_in"] == round(7.62 / 25.4, 4)


def test_garbage_is_safe():
    for bad in (None, {}, {"daily": "nope"}, {"daily": {}}, "x"):
        r = parse_open_meteo(bad)
        assert r["eto_week_in"] is None and r["days"] == 0
