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


def test_rejects_path_traversal_ids():
    # Security gate: the id is a filesystem path segment for the photo dir, so
    # anything that could escape it (../, slash, absolute, NUL, dot) must be
    # rejected on construct AND on .storage load (from_dict -> __post_init__).
    for bad in ("../evil", "..", "a/b", "/abs", "a\\b", "a.b", "x" * 65, "p\x00"):
        with pytest.raises(ValueError):
            _rec(pid=bad)
        with pytest.raises(ValueError):
            PlantRecord.from_dict({**_rec().to_dict(), "id": bad})


def test_accepts_uuid_hex_id():
    # Our own ids are uuid4().hex[:12] = [0-9a-f]{12}; must always pass.
    r = _rec(pid="a1b2c3d4e5f6")
    assert r.id == "a1b2c3d4e5f6"


def test_from_serializable_drops_unsafe_id_record():
    # A crafted store entry with a traversal id is skipped, not loaded.
    good = _rec("p1", name="Lemon").to_dict()
    evil = {**_rec().to_dict(), "id": "../../escape"}
    restored = PlantStore.from_serializable([good, evil])
    assert [p.id for p in restored.all()] == ["p1"]


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


# ── v1.33 vision-health field ───────────────────────────────────────


def test_health_defaults_none_and_round_trips():
    assert _rec().health is None
    health = {
        "health_state": "healthy",
        "confidence": 0.9,
        "assessed_at": "2026-05-18T06:00:00+00:00",
    }
    r = PlantRecord("p1", "Lemon", "high", 100.0, "switch.citrus", health=health)
    rt = PlantRecord.from_dict(r.to_dict())
    assert rt.health == health
    assert rt == r


def test_health_non_dict_rejected_on_construct():
    import pytest

    with pytest.raises(ValueError):
        PlantRecord("p1", "Lemon", "high", 100.0, "switch.citrus", health="not-a-dict")


def test_pre_v133_record_loads_without_health():
    data = _rec().to_dict()
    data.pop("health", None)
    assert PlantRecord.from_dict(data).health is None
    # a corrupt non-dict health on load is tolerated -> None
    data["health"] = "junk"
    assert PlantRecord.from_dict(data).health is None


# ── v1.32 photo history ─────────────────────────────────────────────


def test_photos_default_empty():
    assert _rec().photos == ()


def test_photos_round_trip():
    photos = ({"ts": 100, "path": "/local/x/100.jpg", "note": "spring"},)
    r = PlantRecord("p1", "Lemon", "high", 100.0, "switch.citrus", photos=photos)
    rt = PlantRecord.from_dict(r.to_dict())
    assert rt.photos == photos
    assert rt == r


def test_pre_v132_record_loads_without_photos():
    data = _rec().to_dict()
    data.pop("photos", None)
    assert PlantRecord.from_dict(data).photos == ()


def test_invalid_photo_entries_filtered_on_load():
    data = _rec().to_dict()
    data["photos"] = [
        {"ts": 1, "path": "/local/a.jpg"},  # valid -> survives
        {"ts": 2},  # no path -> dropped
        "junk",  # not a dict -> dropped
        {"path": ""},  # empty path -> dropped
        {"ts": 3, "path": "javascript:alert(1)"},  # non-/local/ scheme -> dropped (XSS guard)
        {"ts": 4, "path": "https://evil.example/x.jpg"},  # absolute URL -> dropped
    ]
    # only the safe "/local/" path survives
    assert PlantRecord.from_dict(data).photos == ({"ts": 1, "path": "/local/a.jpg"},)


def test_pathless_photo_rejected_on_construct():
    import pytest

    with pytest.raises(ValueError):
        PlantRecord("p1", "Lemon", "high", 100.0, "switch.citrus", photos=({"ts": 1},))


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


# ── v1.40.7 — full auto-ID attribute set ─────────────────────────────


def test_id_attributes_roundtrip():
    rec = PlantRecord(
        id="p1",
        name="Front oleander",
        wucols_category="low",
        canopy_area_sqft=40.0,
        zone_entity_id="switch.shrubs",
        common_name="Oleander",
        sunlight_class="full_sun",
        temp_low_f=20,
        temp_high_f=110,
        care_plan_preset="shrub",
        water_every_days=7,
        fertilize_every_days=0,
        id_confidence=0.82,
        id_model="qwen2.5vl:7b",
        id_note="A hardy evergreen shrub.",
        identified_at="2026-07-12T20:00:00",
    )
    back = PlantRecord.from_dict(rec.to_dict())
    assert back.common_name == "Oleander"
    assert back.sunlight_class == "full_sun"
    assert (back.temp_low_f, back.temp_high_f) == (20, 110)
    assert back.care_plan_preset == "shrub"
    assert back.water_every_days == 7
    assert back.fertilize_every_days == 0
    assert back.id_confidence == 0.82
    assert back.id_model == "qwen2.5vl:7b"
    assert back.id_note == "A hardy evergreen shrub."
    assert back.identified_at == "2026-07-12T20:00:00"


def test_old_record_without_id_attributes_loads_with_defaults():
    # A pre-v1.40.7 record has none of the new keys -> empty/None defaults, no crash.
    old = _rec("p1").to_dict()
    for k in (
        "common_name",
        "sunlight_class",
        "temp_low_f",
        "temp_high_f",
        "care_plan_preset",
        "water_every_days",
        "fertilize_every_days",
        "id_confidence",
        "id_model",
        "id_note",
        "identified_at",
    ):
        old.pop(k, None)
    back = PlantRecord.from_dict(old)
    assert back.common_name == "" and back.id_model == "" and back.identified_at == ""
    assert back.temp_low_f is None and back.temp_high_f is None
    assert back.id_confidence is None and back.water_every_days is None


def test_bad_temp_pair_sanitizes_to_none_on_load():
    # A tampered/garbage temp pair (inverted, or only one set) degrades to None
    # rather than dropping the record.
    for bad in ({"temp_low_f": 120, "temp_high_f": 40}, {"temp_low_f": 40}, {"temp_high_f": 40}):
        data = {**_rec().to_dict(), **bad}
        back = PlantRecord.from_dict(data)
        assert back.temp_low_f is None and back.temp_high_f is None


def test_duplicate_copies_care_but_not_photos():
    # v1.40.11 — duplicate_plant does replace(src, new id, cleared photos/map/...).
    # This locks the copy semantics the handler relies on.
    from dataclasses import replace

    src = PlantRecord(
        id="p1",
        name="Oleander",
        wucols_category="low",
        canopy_area_sqft=20.0,
        zone_entity_id="switch.shrubs",
        species="Oleander (Nerium oleander)",
        emitter_count=2,
        emitter_gph=2.0,
        photos=({"path": "/local/x.jpg"},),
        sunlight_class="full_sun",
        temp_low_f=15,
        temp_high_f=115,
        care_plan_preset="shrub",
        water_every_days=10,
        map_x=0.5,
        map_y=0.5,
    )
    new = replace(
        src,
        id="p2",
        name=f"{src.name} (copy)",
        photos=(),
        map_x=None,
        map_y=None,
        health=None,
        species_suggestion=None,
        light_surveys=(),
    )
    # copied
    assert (new.id, new.name) == ("p2", "Oleander (copy)")
    assert new.species == src.species and new.wucols_category == "low"
    assert new.canopy_area_sqft == 20.0 and new.zone_entity_id == "switch.shrubs"
    assert (new.emitter_count, new.emitter_gph) == (2, 2.0)
    assert new.sunlight_class == "full_sun" and new.care_plan_preset == "shrub"
    assert (new.temp_low_f, new.temp_high_f) == (15, 115)
    # NOT copied (fresh photo + placement)
    assert new.photos == () and new.map_x is None and new.map_y is None


# ── v1.54 light-area grouping ───────────────────────────────────────


def _placed(pid, x, y, area=""):
    """A placed plant with a light-area label (canopy area fixed at 50 ft²)."""
    return PlantRecord(
        id=pid,
        name=pid,
        wucols_category="low",
        canopy_area_sqft=50.0,
        zone_entity_id="switch.z",
        map_x=x,
        map_y=y,
        area=area,
    )


def test_area_defaults_empty_and_round_trips():
    r = _placed("p1", 0.5, 0.5, area="Front Bed")
    assert r.area == "Front Bed"
    assert PlantRecord.from_dict(r.to_dict()).area == "Front Bed"
    assert _placed("p2", 0.1, 0.1).area == ""


def test_area_rejects_overlong():
    with pytest.raises(ValueError):
        _placed("p1", 0.5, 0.5, area="x" * 61)


def test_from_dict_tolerant_area():
    base = _placed("p1", 0.5, 0.5).to_dict()
    base.pop("area", None)
    assert PlantRecord.from_dict(base).area == ""
    base["area"] = "y" * 200
    assert len(PlantRecord.from_dict(base).area) == 60


def test_store_by_area_and_areas():
    store = PlantStore()
    for p in (
        _placed("a", 0.1, 0.1, area="Front Bed"),
        _placed("b", 0.2, 0.2, area="Front Bed"),
        _placed("c", 0.9, 0.9, area="Back Corner"),
        _placed("d", 0.5, 0.5),
    ):
        store.upsert(p)
    assert [p.id for p in store.by_area("Front Bed")] == ["a", "b"]
    assert [p.id for p in store.by_area("Back Corner")] == ["c"]
    assert [p.id for p in store.by_area("")] == ["d"]
    assert store.areas() == ["Front Bed", "Back Corner"]


def test_plants_in_region_encloses_and_skips_unplaced():
    from custom_components.complete_irrigation.plant import plants_in_region

    plants = [
        _placed("inside", 0.30, 0.30),
        _placed("edge", 0.20, 0.20),
        _placed("outside", 0.90, 0.90),
        PlantRecord(
            id="noplace",
            name="noplace",
            wucols_category="low",
            canopy_area_sqft=50.0,
            zone_entity_id="switch.z",
        ),
    ]
    ids = plants_in_region(plants, 0.40, 0.40, 0.20, 0.20)
    assert set(ids) == {"inside", "edge"}
    assert "outside" not in ids and "noplace" not in ids
