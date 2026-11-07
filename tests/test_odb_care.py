"""Outdoor Dream Builder rows merged into curated care."""

from __future__ import annotations

from custom_components.complete_irrigation.care_tasks import CARE_PLAN_PRESETS
from custom_components.complete_irrigation.hydraulics import WUCOLS_FACTORS
from custom_components.complete_irrigation.odb_care import (
    ODB_CATEGORY_TO_PRESET,
    ODB_SUN_TO_ENUM,
    ODB_WATER_TO_WUCOLS,
    USDA_ZONE_MIN_F,
    hot_season_water_days,
    usda_zone_minimum_f,
)
from custom_components.complete_irrigation.plant_care_db import (
    BASE_CARE,
    CURATED_CARE,
    ODB_MERGE_REPORT,
    lookup_care,
)
from custom_components.complete_irrigation.species_id import SUNLIGHT_LUX

_CHANGED = {
    ("caesalpinia pulcherrima", "water_days", 14, 7),
    ("carissa macrocarpa", "water_days", 10, 7),
    ("carissa macrocarpa", "temp_low", 25, 20),
    ("chilopsis linearis", "temp_low", 10, 20),
    ("dodonaea viscosa", "water_days", 14, 7),
    ("dodonaea viscosa", "temp_low", 15, 20),
    ("encelia farinosa", "temp_low", 15, 20),
    ("fouquieria splendens", "temp_low", 10, 20),
    ("hesperaloe parviflora", "temp_low", 0, 20),
    ("larrea tridentata", "temp_low", 0, 20),
    ("leucophyllum frutescens", "temp_low", 10, 20),
    ("nerium oleander", "water_days", 10, 7),
    ("nerium oleander", "temp_low", 15, 20),
    ("olea europaea", "temp_low", 15, 20),
    ("phoenix dactylifera", "water_days", 14, 7),
    ("phoenix dactylifera", "temp_low", 15, 20),
    ("punica granatum", "water_days", 10, 7),
    ("punica granatum", "temp_low", 10, 20),
    ("salvia farinacea", "water_days", 10, 7),
    ("salvia farinacea", "temp_low", 10, 20),
    ("salvia greggii", "water_days", 10, 7),
    ("salvia greggii", "temp_low", 15, 20),
    ("tecoma stans", "water_days", 10, 7),
    ("dasylirion wheeleri", "temp_low", 5, 20),
    ("lantana montevidensis", "temp_low", 25, 20),
    ("baileya multiradiata", "temp_low", 15, 20),
}


def test_mapping_tables_use_existing_enums_and_usda_minimums():
    assert ODB_WATER_TO_WUCOLS == {
        "Very Low (Xeric)": "very_low",
        "Low": "low",
        "Moderate": "moderate",
        "High": "high",
    }
    assert set(ODB_WATER_TO_WUCOLS.values()) <= set(WUCOLS_FACTORS)
    assert set(ODB_SUN_TO_ENUM.values()) <= set(SUNLIGHT_LUX)
    assert ODB_SUN_TO_ENUM["full sun"] == "full_sun"
    assert ODB_SUN_TO_ENUM["partial shade"] == "partial_sun"
    assert set(ODB_CATEGORY_TO_PRESET.values()) <= set(CARE_PLAN_PRESETS)
    # USDA PHZM: zone 1 starts at -60 F and each zone is 10 F wide. Zone 9 is 20 F.
    assert USDA_ZONE_MIN_F[1] == -60
    assert USDA_ZONE_MIN_F[8] == 10
    assert USDA_ZONE_MIN_F[9] == 20
    assert USDA_ZONE_MIN_F[11] == 40
    assert usda_zone_minimum_f("USDA 9-11") == 20
    assert usda_zone_minimum_f("USDA 8-11") == 10


def test_hot_season_interval_uses_the_shorter_end():
    assert hot_season_water_days("Water every 2-3 weeks.") == 14
    assert hot_season_water_days("Deep-soak every 10-14 days in summer heat.") == 10
    summer = (
        "Deep-soak weekly through the first summer to establish, then a deep soak "
        "every 2-3 weeks in summer heat and monthly in winter."
    )
    assert hot_season_water_days(summer) == 14
    cool = (
        "Water every 2-3 weeks through the cool season and sparingly in summer; "
        "well-drained soil is essential to prevent crown rot."
    )
    assert hot_season_water_days(cool) is None
    establish = (
        "Established plants need no irrigation; water a new transplant every "
        "1-2 weeks the first spring to trigger leaf-out."
    )
    assert hot_season_water_days(establish) is None


def test_every_odb_row_passes_care_validation():
    derived = [rec for rec in CURATED_CARE.values() if "source" in rec]
    assert derived
    for rec in derived:
        assert rec["wucols"] in WUCOLS_FACTORS
        assert rec["preset"] in CARE_PLAN_PRESETS
        assert rec["sun"] in SUNLIGHT_LUX
        assert rec["temp_low"] < rec["temp_high"]
        assert 1 <= rec["water_days"] <= 60
        assert 0 <= rec["fert_days"] <= 3650
        assert len(rec["note"]) <= 240
        assert rec["source"].startswith("https://www.outdoordreambuilder.com/plants/")


def test_canyon_penstemon_species_row_uses_usda_zone_9():
    care = lookup_care("Canyon Penstemon")
    assert care["common"] == "Canyon Penstemon"
    assert care["temp_low"] == 20
    assert care["wucols"] == "low"
    assert care["sun"] == "full_sun"
    assert care["source"].endswith("/canyon-penstemon")
    assert "water_days" in care["defaulted"]
    assert "temp_high" in care["defaulted"]
    assert lookup_care("Penstemon pseudospectabilis") == care
    # The genus row stays, for other penstemons.
    assert lookup_care("Penstemon") == BASE_CARE["penstemon"]
    assert lookup_care("Penstemon")["water_days"] == 10
    assert "source" not in lookup_care("Penstemon")


def test_existing_rows_are_never_watered_less_often():
    assert len(BASE_CARE) == 82
    assert len(CURATED_CARE) == len(BASE_CARE) + len(ODB_MERGE_REPORT["added"])
    assert len(ODB_MERGE_REPORT["added"]) == 187
    for key, original in BASE_CARE.items():
        assert CURATED_CARE[key]["water_days"] <= original["water_days"], key
    changed = {
        (row["key"], row["field"], row["before"], row["after"])
        for row in ODB_MERGE_REPORT["changed"]
    }
    assert changed == _CHANGED
    skipped = {
        (row["key"], row["water_days"], row["odb_water_days"])
        for row in ODB_MERGE_REPORT["not_applied_would_reduce"]
    }
    assert skipped == {("encelia farinosa", 21, 30), ("hesperaloe parviflora", 21, 30)}
    assert len(ODB_MERGE_REPORT["defaulted_temp_high"]) == 187
    assert len(ODB_MERGE_REPORT["defaulted_water_days"]) == 29
    assert ODB_MERGE_REPORT["skipped"] == []
    # Already at the USDA minimum and the same cadence, so the row is untouched.
    assert CURATED_CARE["carnegiea gigantea"] == BASE_CARE["carnegiea gigantea"]
