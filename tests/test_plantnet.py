"""Pl@ntNet response parsing (v1.51) — pure, real response shape."""

from __future__ import annotations

from custom_components.complete_irrigation.plantnet import parse_plantnet, plantnet_url

REAL = {
    "remainingIdentificationRequests": 499,
    "results": [
        {
            "score": 0.876,
            "species": {
                "scientificNameWithoutAuthor": "Nerium oleander",
                "commonNames": ["Oleander", "Rose laurel"],
                "genus": {"scientificNameWithoutAuthor": "Nerium"},
                "family": {"scientificNameWithoutAuthor": "Apocynaceae"},
            },
        },
        {
            "score": 0.04,
            "species": {"scientificNameWithoutAuthor": "Nerium indicum", "commonNames": []},
        },
    ],
}


def test_url_has_key_as_query_no_body():
    u = plantnet_url("abc123")
    assert u.startswith("https://my-api.plantnet.org/v2/identify/all")
    assert "api-key=abc123" in u


def test_parse_top_result():
    r = parse_plantnet(REAL)
    assert r["matched"]
    assert r["species"] == "Nerium oleander"
    assert r["common_name"] == "Oleander"
    assert r["confidence"] == 0.876
    assert r["remaining"] == 499
    assert "Nerium indicum" in r["alternatives"]


def test_parse_no_results():
    assert parse_plantnet({"results": []})["matched"] is False
    assert parse_plantnet({"results": [{"score": 0.1, "species": {}}]})["matched"] is False


def test_parse_garbage():
    for bad in (None, {}, "x", {"results": "nope"}):
        assert parse_plantnet(bad)["matched"] is False


def test_missing_score_is_zero_not_crash():
    r = parse_plantnet({"results": [{"species": {"scientificNameWithoutAuthor": "Rosa"}}]})
    assert r["matched"] and r["species"] == "Rosa" and r["confidence"] == 0.0
