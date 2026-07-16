"""Perenual species-list parsing (v1.52) — pure, real response shape."""

from __future__ import annotations

from custom_components.complete_irrigation.perenual import (
    parse_perenual_search,
    perenual_search_url,
)

# Trimmed to the fields the parser reads; mirrors a real species-list row.
REAL = {
    "data": [
        {
            "id": 2,
            "common_name": "European Silver Fir",
            "scientific_name": ["Abies alba"],
            "cycle": "Perennial",
            "watering": "Frequent",
            "sunlight": ["full_sun", "part_shade"],
        },
        {
            "id": 3,
            "common_name": "Balsam Fir",
            "scientific_name": ["Abies balsamea"],
            "watering": "Average",
            "sunlight": ["part_shade"],
        },
    ]
}


def test_url_has_key_and_query():
    u = perenual_search_url("abc123", "Abies alba")
    assert u.startswith("https://perenual.com/api/v2/species-list")
    assert "key=abc123" in u
    assert "q=Abies%20alba" in u


def test_parse_top_result_maps_watering_and_sun():
    r = parse_perenual_search(REAL)
    assert r["matched"]
    assert r["species"] == "Abies alba"
    assert r["common_name"] == "European Silver Fir"
    assert r["confidence"] == 0.6
    assert r["wucols_category"] == "high"  # Frequent -> high
    assert r["water_every_days"] == 3
    assert r["sunlight_class"] == "full_sun"  # brightest-wins over part_shade
    assert "Perennial" in r["note"]


def test_watering_buckets():
    def wu(w):
        return parse_perenual_search({"data": [{"scientific_name": ["X y"], "watering": w}]})[
            "wucols_category"
        ]

    assert wu("Average") == "moderate"
    assert wu("Minimum") == "low"
    assert wu("None") == "very_low"
    assert wu("weird") is None  # unknown watering -> dropped, not guessed


def test_sunlight_mapping():
    def sun(tokens):
        return parse_perenual_search({"data": [{"scientific_name": ["X y"], "sunlight": tokens}]})[
            "sunlight_class"
        ]

    assert sun(["part_shade"]) == "partial_sun"
    assert sun(["filtered_shade"]) == "partial_sun"
    assert sun(["deep_shade"]) == "deep_shade"
    assert sun(["full_shade"]) == "bright_shade"
    assert sun([]) is None


def test_scientific_name_string_fallback_and_common_only():
    # scientific_name given as a bare string
    r = parse_perenual_search({"data": [{"scientific_name": "Rosa woodsii"}]})
    assert r["matched"] and r["species"] == "Rosa woodsii"
    # only a common name -> use it as the species text
    r2 = parse_perenual_search({"data": [{"common_name": "Woods Rose"}]})
    assert r2["matched"] and r2["species"] == "Woods Rose"


def test_parse_garbage():
    for bad in (None, {}, "x", {"data": "nope"}, {"data": []}, {"data": [{}]}):
        assert parse_perenual_search(bad)["matched"] is False
