"""Outdoor Dream Builder plant reference. Read-only, off the watering path."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from custom_components.complete_irrigation.hydraulics import WUCOLS_FACTORS
from custom_components.complete_irrigation.plant_care_db import CURATED_CARE, lookup_care
from custom_components.complete_irrigation.reference_plants import (
    REFERENCE_PATH,
    load_reference,
    lookup_reference,
)

_URL = re.compile(r"https://www.outdoordreambuilder.com/plants/[a-z0-9-]+")
_FIELDS = (
    "source_url",
    "fetched_at",
    "common_name",
    "botanical_name",
    "water_needs",
    "sun",
    "mature_size",
    "bloom_season",
    "bloom_color",
    "hardiness",
    "watering_guidance",
)
# Water classes as Outdoor Dream Builder prints them. Not WUCOLS keys.
_WATER_CLASSES = {"Very Low (Xeric)", "Low", "Moderate"}
_SUN_VALUES = {"full sun", "partial shade", "any"}
_NOTE = "Quick Stats lists {quick}; the page narrative lists {narrative}. Both are kept as printed."
_PENSTEMON = {
    "common": "Penstemon",
    "wucols": "low",
    "sun": "full_sun",
    "temp_low": 0,
    "temp_high": 110,
    "preset": "flower",
    "water_days": 10,
    "fert_days": 0,
    "note": "Desert wildflower; low water, spring bloom.",
}
_OLEANDER = {
    "common": "Oleander",
    "wucols": "low",
    "sun": "full_sun",
    "temp_low": 15,
    "temp_high": 115,
    "preset": "shrub",
    "water_days": 10,
    "fert_days": 0,
    "note": "Very drought-tolerant flowering shrub (all parts toxic).",
}
_NATAL_PLUM = {
    "common": "Natal Plum",
    "wucols": "low",
    "sun": "full_sun",
    "temp_low": 25,
    "temp_high": 110,
    "preset": "shrub",
    "water_days": 10,
    "fert_days": 60,
    "note": "Dense glossy evergreen shrub with fragrant white flowers.",
}


def test_dataset_loads_207_unique_source_urls():
    raw = json.loads(REFERENCE_PATH.read_text(encoding="utf-8"))
    loaded = load_reference()
    assert isinstance(raw, list)
    assert len(raw) == 207
    assert len(loaded) == 207
    urls = [rec["source_url"] for rec in loaded]
    assert len(urls) == len(set(urls))
    assert all(_URL.fullmatch(url) for url in urls)
    assert all(url.startswith("https://www.outdoordreambuilder.com/plants/") for url in urls)


def test_records_keep_page_values_and_drop_page_text():
    for rec in load_reference():
        assert tuple(rec) == _FIELDS
        for key in (
            "source_url",
            "fetched_at",
            "common_name",
            "botanical_name",
            "water_needs",
            "sun",
            "mature_size",
            "watering_guidance",
        ):
            assert isinstance(rec[key], str) and rec[key].strip()
        assert rec["fetched_at"].startswith("2026-11-07")
        assert rec["water_needs"] in _WATER_CLASSES
        assert rec["water_needs"] not in WUCOLS_FACTORS
        assert "wucols" not in rec
        assert rec["sun"] in _SUN_VALUES
        assert rec["sun"] not in {"full_sun", "partial_sun", "bright_shade", "deep_shade"}
        assert "page_text" not in rec
        assert "Companion" not in rec["watering_guidance"]
        hardiness = rec["hardiness"]
        assert set(hardiness) == {"quick_stats", "narrative", "note"}
        assert isinstance(hardiness["quick_stats"], str) and hardiness["quick_stats"]
        if hardiness["narrative"] is None:
            assert hardiness["note"] is None
        else:
            assert hardiness["note"] == _NOTE.format(
                quick=hardiness["quick_stats"],
                narrative=hardiness["narrative"],
            )
            assert hardiness["narrative"] != hardiness["quick_stats"]


def test_not_stated_bloom_fields_stay_null():
    missing_season = set()
    missing_color = set()
    for rec in load_reference():
        if rec["bloom_season"] is None:
            missing_season.add(rec["common_name"])
        if rec["bloom_color"] is None:
            missing_color.add(rec["common_name"])
    assert missing_season == {"Date Palm", "Century Plant"}
    assert missing_color == {"Date Palm", "Century Plant"}
    date = lookup_reference("date palm")
    century = lookup_reference("century plant")
    assert date["botanical_name"] == "Phoenix dactylifera"
    assert century["botanical_name"] == "Agave americana"
    assert date["water_needs"] == "Moderate"
    assert century["hardiness"]["quick_stats"] == "USDA 8-11"
    assert century["hardiness"]["narrative"] == "Phoenix Zone 8b-10a"


def test_canyon_penstemon_keeps_both_hardiness_values():
    rec = lookup_reference("Canyon Penstemon")
    assert rec["common_name"] == "Canyon Penstemon"
    assert rec["botanical_name"] == "Penstemon pseudospectabilis"
    assert rec["source_url"] == "https://www.outdoordreambuilder.com/plants/canyon-penstemon"
    assert rec["water_needs"] == "Low"
    assert rec["sun"] == "full sun"
    assert rec["mature_size"] == "1.5 ft H \u00d7 2 ft W"
    assert rec["bloom_season"] == "Spring"
    assert rec["bloom_color"] == "Rose-purple"
    assert rec["hardiness"]["quick_stats"] == "USDA 9-11"
    assert rec["hardiness"]["narrative"] == "USDA zones 8-11"
    assert "USDA 9-11" in rec["hardiness"]["note"]
    assert "USDA zones 8-11" in rec["hardiness"]["note"]
    assert rec["watering_guidance"] == (
        "Water every 2-3 weeks through the cool season and sparingly in summer; "
        "well-drained soil is essential to prevent crown rot."
    )
    # The same page, addressed by its botanical name.
    assert lookup_reference("penstemon pseudospectabilis")["common_name"] == "Canyon Penstemon"


def test_lookup_is_case_insensitive_and_unknown_names_are_none():
    assert lookup_reference("canyon penstemon")["botanical_name"] == "Penstemon pseudospectabilis"
    assert lookup_reference("CANYON PENSTEMON")["water_needs"] == "Low"
    assert lookup_reference("  Penstemon Pseudospectabilis  ")["sun"] == "full sun"
    assert lookup_reference("Penstemon") is None
    assert lookup_reference("not a plant") is None
    assert lookup_reference("") is None
    assert lookup_reference("   ") is None
    assert lookup_reference(None) is None
    assert lookup_reference(12) is None


def test_lookup_returns_a_copy():
    first = lookup_reference("Canyon Penstemon")
    first["water_needs"] = "changed"
    first["hardiness"]["narrative"] = "changed"
    second = lookup_reference("Canyon Penstemon")
    assert second["water_needs"] == "Low"
    assert second["hardiness"]["narrative"] == "USDA zones 8-11"
    assert first is not second


def test_curated_care_and_lookup_care_are_unchanged():
    assert len(CURATED_CARE) == 82
    assert lookup_care("Nerium oleander") == _OLEANDER
    assert lookup_care("Carissa macrocarpa") == _NATAL_PLUM
    # The species string contains the curated genus key, so the operational
    # lookup still returns that genus row. It does not return the reference page.
    assert lookup_care("Penstemon pseudospectabilis") == _PENSTEMON
    assert lookup_care("Canyon Penstemon") == _PENSTEMON
    assert lookup_care("Oleander") is None
    assert "source_url" not in lookup_care("Penstemon pseudospectabilis")
    for rec in CURATED_CARE.values():
        assert "source_url" not in rec
        assert "watering_guidance" not in rec


def test_reference_loader_is_not_wired_into_runtime_modules():
    root = Path(REFERENCE_PATH).parent.parent
    for path in root.rglob("*.py"):
        if path.name == "reference_plants.py":
            continue
        text = path.read_text(encoding="utf-8")
        assert "lookup_reference" not in text, path
        assert "outdoordreambuilder_plants" not in text, path
        assert "reference_plants" not in text, path
    tree = ast.parse((root / "reference_plants.py").read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    assert imported == ["__future__", "copy", "json", "pathlib", "typing"]
