"""GBIF species-name verification parsing (v1.46) — pure, no network in tests."""

from __future__ import annotations

from custom_components.complete_irrigation.species_name import (
    gbif_match_url,
    parse_gbif_match,
)

# Real GBIF response shapes (verified against api.gbif.org 2026-07-16).
EXACT = {
    "canonicalName": "Lantana camara",
    "scientificName": "Lantana camara L.",
    "rank": "SPECIES",
    "status": "ACCEPTED",
    "confidence": 99,
    "matchType": "EXACT",
    "family": "Verbenaceae",
}
FUZZY = {  # "Nerium oleandr" typo
    "canonicalName": "Nerium oleander",
    "scientificName": "Nerium oleander L.",
    "rank": "SPECIES",
    "status": "ACCEPTED",
    "confidence": 95,
    "matchType": "FUZZY",
    "family": "Apocynaceae",
}
HIGHERRANK = {
    "canonicalName": "Salvia",
    "rank": "GENUS",
    "status": "ACCEPTED",
    "confidence": 92,
    "matchType": "HIGHERRANK",
}
NONE = {"matchType": "NONE", "confidence": 100, "synonym": False}
WITH_ALTS = {
    "canonicalName": "Ficus microcarpa",
    "rank": "SPECIES",
    "status": "ACCEPTED",
    "matchType": "FUZZY",
    "confidence": 80,
    "alternatives": [
        {"canonicalName": "Ficus benjamina", "rank": "SPECIES"},
        {"canonicalName": "Ficus microcarpa", "rank": "SPECIES"},  # dup of main -> dropped
        {"canonicalName": "Ficus carica", "rank": "SPECIES"},
    ],
}


def test_url_encodes_the_name_and_restricts_to_plants():
    u = gbif_match_url("Nerium oleander")
    assert u.startswith("https://api.gbif.org/v1/species/match")
    assert "name=Nerium%20oleander" in u
    assert "kingdomKey=6" in u  # Plantae — no animal homonyms


def test_exact_match_is_verified():
    r = parse_gbif_match(EXACT)
    assert r["matched"] and r["exact"]
    assert r["canonical"] == "Lantana camara"
    assert r["family"] == "Verbenaceae"
    assert "Verified" in r["note"]


def test_fuzzy_match_flags_a_typo_correction():
    r = parse_gbif_match(FUZZY)
    assert r["matched"] and not r["exact"]
    assert r["canonical"] == "Nerium oleander"
    assert r["confidence"] == 95
    assert "typo" in r["note"].lower()


def test_higherrank_asks_for_the_species():
    r = parse_gbif_match(HIGHERRANK)
    assert r["matched"] and not r["exact"]
    assert r["rank"] == "GENUS"
    assert "species" in r["note"].lower()


def test_no_match_returns_unmatched():
    r = parse_gbif_match(NONE)
    assert not r["matched"] and r["match_type"] == "NONE"
    assert "no plant match" in r["note"].lower()


def test_alternatives_are_deduped_and_capped():
    r = parse_gbif_match(WITH_ALTS)
    assert "Ficus microcarpa" not in r["alternatives"]  # equals the main match
    assert "Ficus benjamina" in r["alternatives"]
    assert len(r["alternatives"]) <= 5


def test_garbage_input_is_safe():
    assert parse_gbif_match(None)["matched"] is False
    assert parse_gbif_match("nonsense")["matched"] is False
    assert parse_gbif_match({})["matched"] is False
