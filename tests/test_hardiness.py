"""USDA hardiness-zone parsing (v1.50) — pure, real phzmapi shape."""

from __future__ import annotations

from custom_components.complete_irrigation.hardiness import (
    frost_tender,
    parse_phzm,
    phzm_url,
    zone_temp_band_f,
)


def test_url_strips_to_digits():
    assert phzm_url("85295") == "https://phzmapi.org/85295.json"
    assert phzm_url("85295-1234") == "https://phzmapi.org/85295.json"
    assert "key" not in phzm_url("85295").lower()


def test_zone_bands():
    assert zone_temp_band_f("9b") == (25, 30)  # Gilbert AZ
    assert zone_temp_band_f("9a") == (20, 25)
    assert zone_temp_band_f("1a") == (-60, -55)
    assert zone_temp_band_f("13b") == (65, 70)
    assert zone_temp_band_f("7A") == (0, 5)  # case-insensitive


def test_zone_band_rejects_junk():
    for bad in ("", "x", "0a", "14a", "9c", None, "abc"):
        assert zone_temp_band_f(bad) is None


def test_parse_real_shape():
    # phzmapi returns {"zone":"9b","coordinates":{...},"temperature_range":"..."}
    r = parse_phzm({"zone": "9b", "coordinates": {"lat": 33.3, "lon": -111.8}})
    assert r["matched"]
    assert r["zone"] == "9b"
    assert r["temp_low_f"] == 25 and r["temp_high_f"] == 30


def test_parse_bad():
    for bad in (None, {}, {"zone": ""}, {"zone": "nope"}, "x"):
        assert parse_phzm(bad)["matched"] is False


def test_frost_tender_logic():
    # Zone 9b coldest = 25 F. A plant hardy only to 30 F -> tender here.
    assert frost_tender(30, 25) is True
    # A plant hardy to 15 F is fine in a zone that only reaches 25 F.
    assert frost_tender(15, 25) is False
    assert frost_tender(25, 25) is False  # equal = ok
    # Missing data never raises a false alarm.
    assert frost_tender(None, 25) is False
    assert frost_tender(30, None) is False
