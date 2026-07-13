"""Curated plant-care lookup (v1.40.12) — the deterministic fix for the LLM's
uniform "moderate" water-use guess."""

from __future__ import annotations

from custom_components.complete_irrigation.hydraulics import WUCOLS_FACTORS
from custom_components.complete_irrigation.plant_care_db import CURATED_CARE, lookup_care


def test_matches_scientific_name_inside_display_string():
    c = lookup_care("Dwarf Oleander (Nerium oleander)")
    assert c and c["wucols"] == "low" and c["preset"] == "shrub"
    c = lookup_care("Lemon (Citrus limon)")
    assert c and c["wucols"] == "moderate" and c["preset"] == "tree"


def test_specific_species_beats_genus_fallback():
    # "citrus limon" must win over the generic "citrus" key.
    assert lookup_care("Citrus limon")["common"] == "Lemon"
    assert lookup_care("Citrus aurantiifolia")["common"] == "Key Lime"
    # a bare citrus still resolves to the genus fallback
    assert lookup_care("some citrus tree")["common"] == "Citrus"


def test_desert_plants_are_low_not_moderate():
    # The whole point: these must NOT come back "moderate" (qwen's uniform guess).
    for name in (
        "Nerium oleander",
        "Lantana camara",
        "Leucophyllum frutescens",
        "Salvia rosmarinus",
    ):
        assert lookup_care(name)["wucols"] == "low"


def test_none_for_uncovered_and_bad_input():
    assert lookup_care("Quercus something-exotic") is None
    assert lookup_care("") is None
    assert lookup_care(None) is None


def test_every_curated_record_is_valid():
    # wucols + preset must be values the rest of the app accepts.
    from custom_components.complete_irrigation.care_tasks import CARE_PLAN_PRESETS

    for key, c in CURATED_CARE.items():
        assert c["wucols"] in WUCOLS_FACTORS, key
        assert c["preset"] in CARE_PLAN_PRESETS, key
        assert c["temp_low"] < c["temp_high"], key
