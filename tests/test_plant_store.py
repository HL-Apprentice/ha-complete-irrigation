"""Tests for PlantRecord + PlantStore (pure logic, v2).

Validation, CRUD, by-zone grouping, round-trip serialization, and the
bridge to the hydraulics calc type.
"""

from __future__ import annotations

import math

import pytest

from custom_components.complete_irrigation.hydraulics import Plant
from custom_components.complete_irrigation.plant import PlantRecord, PlantStore


def _rec(pid="p1", name="Lemon", cat="high", area=100.0, zone="switch.citrus") -> PlantRecord:
    return PlantRecord(
        id=pid, name=name, wucols_category=cat, canopy_area_sqft=area, zone_entity_id=zone
    )


# ── PlantRecord validation ──────────────────────────────────────────


def test_valid_record():
    r = _rec()
    assert r.id == "p1"
    assert r.wucols_category == "high"


def test_rejects_unknown_category():
    with pytest.raises(ValueError):
        _rec(cat="ultra")


def test_rejects_nonpositive_area():
    with pytest.raises(ValueError):
        _rec(area=0)
    with pytest.raises(ValueError):
        _rec(area=-5)


def test_rejects_nonfinite_and_oversized_area():
    # Security-gate hardening: NaN/Inf (which slip past a bare "<= 0" check)
    # and absurd values must be rejected on the .storage load path too.
    for bad in (math.nan, math.inf, -math.inf, 1_000_000):
        with pytest.raises(ValueError):
            _rec(area=bad)


def test_from_dict_rejects_nonfinite_area():
    data = _rec().to_dict()
    data["canopy_area_sqft"] = float("nan")
    with pytest.raises(ValueError):
        PlantRecord.from_dict(data)


def test_rejects_blank_name_and_ids():
    with pytest.raises(ValueError):
        _rec(name="  ")
    with pytest.raises(ValueError):
        _rec(pid="")
    with pytest.raises(ValueError):
        _rec(zone="")


def test_round_trip_serialization():
    r = _rec()
    assert PlantRecord.from_dict(r.to_dict()) == r


def test_from_dict_ignores_unknown_future_keys():
    data = _rec().to_dict()
    data["location"] = {"x": 0.4, "y": 0.7}  # a field a later version might add
    data["installed_emitters"] = [{"count": 2, "gph": 2}]
    rec = PlantRecord.from_dict(data)  # must not raise
    assert rec.name == "Lemon"


def test_to_calc_plant_bridges_to_hydraulics():
    r = _rec(zone="switch.citrus")
    p = r.to_calc_plant()
    assert isinstance(p, Plant)
    assert p.loop_id == "switch.citrus"
    assert p.plant_factor == 0.8  # high


# ── v1.30 map coordinates ───────────────────────────────────────────


def test_map_coords_default_to_unplaced():
    r = _rec()
    assert r.map_x is None and r.map_y is None


def test_map_coords_round_trip():
    r = PlantRecord("p1", "Lemon", "high", 100.0, "switch.citrus", map_x=0.25, map_y=0.8)
    assert PlantRecord.from_dict(r.to_dict()) == r


def test_pre_v130_record_loads_as_unplaced():
    data = _rec().to_dict()
    data.pop("map_x", None)
    data.pop("map_y", None)
    rec = PlantRecord.from_dict(data)  # must not raise
    assert rec.map_x is None and rec.map_y is None


def test_map_coords_out_of_range_rejected():
    import pytest

    with pytest.raises(ValueError):
        PlantRecord("p1", "Lemon", "high", 100.0, "switch.citrus", map_x=1.4, map_y=0.5)
    with pytest.raises(ValueError):
        PlantRecord("p1", "Lemon", "high", 100.0, "switch.citrus", map_x=0.5, map_y=-0.1)


# ── PlantStore CRUD ─────────────────────────────────────────────────


def test_add_and_get():
    s = PlantStore()
    s.add(_rec("p1"))
    assert s.get("p1").name == "Lemon"
    assert s.get("missing") is None


def test_add_duplicate_id_raises():
    s = PlantStore()
    s.add(_rec("p1"))
    with pytest.raises(KeyError):
        s.add(_rec("p1"))


def test_upsert_replaces():
    s = PlantStore()
    s.add(_rec("p1", name="Lemon"))
    s.upsert(_rec("p1", name="Orange"))
    assert s.get("p1").name == "Orange"
    assert len(s.all()) == 1


def test_delete():
    s = PlantStore()
    s.add(_rec("p1"))
    assert s.delete("p1") is True
    assert s.delete("p1") is False
    assert s.all() == []


def test_all_preserves_insertion_order():
    s = PlantStore()
    s.add(_rec("p1", name="A"))
    s.add(_rec("p2", name="B"))
    s.add(_rec("p3", name="C"))
    assert [p.name for p in s.all()] == ["A", "B", "C"]


# ── grouping by zone/loop ───────────────────────────────────────────


def test_by_zone_and_zone_ids():
    s = PlantStore()
    s.add(_rec("p1", zone="switch.citrus"))
    s.add(_rec("p2", zone="switch.citrus"))
    s.add(_rec("p3", zone="switch.shrubs"))
    assert [p.id for p in s.by_zone("switch.citrus")] == ["p1", "p2"]
    assert s.by_zone("switch.none") == []
    assert s.zone_ids() == ["switch.citrus", "switch.shrubs"]


# ── store serialization round-trip ──────────────────────────────────


def test_store_round_trip():
    s = PlantStore()
    s.add(_rec("p1", name="Lemon", zone="switch.citrus"))
    s.add(_rec("p2", name="Saguaro", cat="very_low", area=9, zone="switch.shrubs"))
    restored = PlantStore.from_serializable(s.to_serializable())
    assert [p.id for p in restored.all()] == ["p1", "p2"]
    assert restored.get("p2").wucols_category == "very_low"


def test_from_serializable_skips_bad_records_without_failing_load():
    # Security-gate hardening: one corrupt entry must not take down the whole
    # store (which would break list_plants / yard_report).
    good = _rec("p1", name="Lemon").to_dict()
    good2 = _rec("p2", name="Orange").to_dict()
    bad_missing_key = {"id": "p3", "name": "X"}  # missing required fields
    bad_value = {**_rec("p4").to_dict(), "wucols_category": "ultra"}
    restored = PlantStore.from_serializable([good, bad_missing_key, good2, bad_value])
    assert [p.id for p in restored.all()] == ["p1", "p2"]  # only the valid ones
