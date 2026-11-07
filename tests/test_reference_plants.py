"""Outdoor Dream Builder page records, including canonical USDA zones."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from custom_components.complete_irrigation.hydraulics import WUCOLS_FACTORS
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
    "usda_zone",
    "hardiness_alt",
    "hardiness_note",
    "category",
    "heat_tolerance_f",
    "fertilizer_guidance",
    "watering_guidance",
)
# Water classes as Outdoor Dream Builder prints them. Not WUCOLS keys.
_WATER_CLASSES = {"Very Low (Xeric)", "Low", "Moderate"}
_SUN_VALUES = {"full sun", "partial shade", "any"}
_NOTE = "Quick Stats lists {quick}; the page narrative lists {narrative}. Both are kept as printed."


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
        assert isinstance(rec["usda_zone"], str) and rec["usda_zone"].startswith("USDA ")
        assert isinstance(rec["category"], str) and rec["category"]
        assert isinstance(rec["fertilizer_guidance"], str) and rec["fertilizer_guidance"]
        if rec["hardiness_alt"] is None:
            assert rec["hardiness_note"] is None
        else:
            assert rec["hardiness_note"] == _NOTE.format(
                quick=rec["usda_zone"],
                narrative=rec["hardiness_alt"],
            )
            assert rec["hardiness_alt"] != rec["usda_zone"]


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
    assert century["usda_zone"] == "USDA 8-11"
    assert century["hardiness_alt"] == "Phoenix Zone 8b-10a"
    alts = [rec for rec in load_reference() if rec["hardiness_alt"]]
    assert len(alts) == 76


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
    assert rec["usda_zone"] == "USDA 9-11"
    assert rec["hardiness_alt"] == "USDA zones 8-11"
    assert "USDA 9-11" in rec["hardiness_note"]
    assert "USDA zones 8-11" in rec["hardiness_note"]
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
    first["hardiness_alt"] = "changed"
    second = lookup_reference("Canyon Penstemon")
    assert second["water_needs"] == "Low"
    assert second["hardiness_alt"] == "USDA zones 8-11"
    assert first is not second


def test_page_loader_is_not_imported_by_schedule_or_rain_code():
    root = Path(REFERENCE_PATH).parent.parent
    allowed = {"reference_plants.py", "odb_care.py"}
    for path in root.rglob("*.py"):
        if path.name in allowed:
            continue
        text = path.read_text(encoding="utf-8")
        assert "lookup_reference" not in text, path
        assert "outdoordreambuilder_plants" not in text, path
        assert "reference_plants" not in text, path
    for name in ("schedule.py", "rain_lockout.py", "zone_region.py"):
        text = (root / name).read_text(encoding="utf-8")
        assert "odb_care" not in text
        assert "lookup_care" not in text
    tree = ast.parse((root / "reference_plants.py").read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    assert imported == ["__future__", "copy", "json", "pathlib", "typing"]
