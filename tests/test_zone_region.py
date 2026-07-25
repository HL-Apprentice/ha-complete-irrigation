"""Zone regions (v1.60) — ground-truth geometry, durability and load hardening.

The contract: a region is FOUR DEGREES OF REAL GROUND plus a loop. Where it
lands on the aerial is derived, never stored — so moving, zooming, nudging or
rotating the frame cannot displace, shrink or lose it.
"""

from __future__ import annotations

import math

import pytest

from custom_components.complete_irrigation.yard_map import (
    bbox_from_center,
    canopy_sqft_from_box,
    offset_center,
    region_sqft_from_box,
)
from custom_components.complete_irrigation.zone_region import (
    ZoneRegion,
    ZoneRegionStore,
    box_intersects_frame,
    derive_box,
    region_at_point,
)

LAT, LON = 33.30, -111.80  # a generic point near Chandler, AZ — not a real address
FRAME = bbox_from_center(LAT, LON, 60.0)

_M_LAT = 111_320.0


def _region(**kw) -> ZoneRegion:
    """A ~20 m x 10 m patch centred on the frame, unless overridden."""
    half_lat = (10.0 / 2) / _M_LAT
    half_lon = (20.0 / 2) / (_M_LAT * math.cos(math.radians(LAT)))
    base = {
        "id": "r1",
        "zone_entity_id": "switch.back_yard_grass_blue",
        "north": LAT + half_lat,
        "south": LAT - half_lat,
        "east": LON + half_lon,
        "west": LON - half_lon,
    }
    base.update(kw)
    return ZoneRegion(**base)


# ── durability: the frame is a window, never the truth ──────────────


def test_region_survives_zoom_cycle_unchanged():
    r = _region()
    before = r.to_dict()
    b0 = derive_box(r, FRAME)
    for span in (30.0, 120.0, 15.0):
        derive_box(r, bbox_from_center(LAT, LON, span))
    assert r.to_dict() == before  # nothing was ever written
    b1 = derive_box(r, FRAME)
    for a, b in zip(b0, b1, strict=True):
        assert abs(a - b) < 1e-9


def test_region_partially_off_frame_is_not_dropped():
    r = _region()
    tight = bbox_from_center(LAT, LON, 8.0)  # narrower than the region
    box = derive_box(r, tight)
    assert box[0] < 0.0 or box[2] > 1.0  # raw/unclamped, not pinned to the edge
    assert box_intersects_frame(box)  # still partly visible
    assert r.to_dict() == _region().to_dict()  # record untouched


def test_region_fully_off_frame_keeps_intact_record():
    r = _region()
    far = bbox_from_center(LAT + 0.01, LON + 0.01, 30.0)  # ~1 km away
    assert not box_intersects_frame(derive_box(r, far))  # panel draws nothing
    assert r.to_dict() == _region().to_dict()  # and nothing is mutated


def test_one_metre_nudge_keeps_ground_position():
    r = _region()
    before = derive_box(r, FRAME)
    lat2, lon2 = offset_center(LAT, LON, 1.0, 0.0)  # frame moves 1 m north
    after = derive_box(r, bbox_from_center(lat2, lon2, 60.0))
    # The region slides DOWN the image by exactly 1 m of the 60 m span.
    assert abs((after[1] - before[1]) - 1.0 / 60.0) < 1e-6
    assert abs(after[0] - before[0]) < 1e-6  # no meaningful sideways drift


def test_derived_box_is_independent_of_rotation_deg():
    # Rotation is a display transform and must never enter the geometry.
    from custom_components.complete_irrigation.yard_map import normalize_rotation

    r = _region()
    box = derive_box(r, FRAME)
    for deg in (0, 45, 90, -33):
        normalize_rotation(deg)
        assert derive_box(r, FRAME) == box


# ── area maths ──────────────────────────────────────────────────────


def test_region_area_matches_true_ground_metres():
    r = _region()  # 20 m x 10 m = 200 m^2
    assert abs(r.width_m - 20.0) < 0.05
    assert abs(r.height_m - 10.0) < 0.05
    assert abs(r.area_sqft - 200 * 10.7639) < 0.5


def test_reported_sqft_invariant_across_zoom():
    r = _region()
    seen = []
    for span in (60.0, 30.0, 120.0):
        x0, y0, x1, y1 = derive_box(r, bbox_from_center(LAT, LON, span))
        seen.append(region_sqft_from_box(x1 - x0, y1 - y0, span))
    assert max(seen) - min(seen) < 0.5  # same ground, any zoom


def test_region_sqft_is_full_rectangle_not_ellipse():
    dx, dy, span = 0.3, 0.2, 60.0
    ratio = region_sqft_from_box(dx, dy, span) / canopy_sqft_from_box(dx, dy, span)
    # both helpers round to 0.1 sq ft, so compare to the rounding floor not to 0
    assert abs(ratio - 4 / math.pi) < 1e-3  # the ellipse under-reports a lawn ~21%


# ── construction + validation ───────────────────────────────────────


def test_from_dict_canonicalizes_corner_order():
    a = _region()
    flipped = ZoneRegion(
        id="r1",
        zone_entity_id="switch.back_yard_grass_blue",
        north=a.south,
        south=a.north,  # upside down
        east=a.west,
        west=a.east,  # and back to front
    )
    assert flipped.to_dict() == a.to_dict()


def test_rejects_degenerate_and_absurd_geometry():
    with pytest.raises(ValueError):  # zero extent
        _region(north=LAT, south=LAT)
    with pytest.raises(ValueError):  # a ~2 cm band is a mis-drag
        _region(north=LAT + 0.0000002, south=LAT)
    with pytest.raises(ValueError):  # bigger than any yard
        _region(north=LAT + 0.05, south=LAT - 0.05, east=LON + 0.05, west=LON - 0.05)


def test_rejects_bad_ids_labels_and_degrees():
    with pytest.raises(ValueError):
        _region(id="../escape")
    with pytest.raises(ValueError):
        _region(zone_entity_id="not-an-entity")
    with pytest.raises(ValueError):
        _region(label="x" * 61)
    with pytest.raises(ValueError):
        _region(label="bad\x00label")
    for bad in (float("nan"), float("inf"), 91.0):
        with pytest.raises(ValueError):
            _region(north=bad)


def test_unknown_schema_value_is_skipped_not_misread():
    d = _region().to_dict()
    d["schema"] = 2
    with pytest.raises(ValueError):
        ZoneRegion.from_dict(d)


# ── store ───────────────────────────────────────────────────────────


def test_store_crud_roundtrip():
    s = ZoneRegionStore()
    s.add(_region(id="a"))
    s.add(_region(id="b", zone_entity_id="switch.front_yard_grass_yellow"))
    with pytest.raises(KeyError):
        s.add(_region(id="a"))
    assert len(s.all()) == 2
    assert [r.id for r in s.by_zone("switch.front_yard_grass_yellow")] == ["b"]
    assert s.delete("a") is True
    assert s.delete("a") is False
    back = ZoneRegionStore.from_serializable(s.to_serializable())
    assert [r.to_dict() for r in back.all()] == [r.to_dict() for r in s.all()]


def test_from_serializable_skips_bad_records_and_keeps_good():
    good = _region(id="good").to_dict()
    bad_batch = [
        good,
        {**good, "id": "nan", "north": float("nan")},
        {**good, "id": "oob", "west": -999.0},
        {**good, "id": "flat", "north": good["south"]},
        {**good, "id": "badzone", "zone_entity_id": "nope"},
        {**good, "id": "../escape"},
        {k: v for k, v in good.items() if k != "north"},  # missing key
        "not-a-dict",
    ]
    store = ZoneRegionStore.from_serializable(bad_batch)
    assert [r.id for r in store.all()] == ["good"]  # no exception, good survives


def test_from_serializable_caps_record_count():
    many = [_region(id=f"r{i}").to_dict() for i in range(250)]
    assert len(ZoneRegionStore.from_serializable(many).all()) == 200


def test_from_serializable_tolerates_junk_input():
    for junk in (None, {}, 42, "x"):
        assert ZoneRegionStore.from_serializable(junk).all() == []


# ── hit testing ─────────────────────────────────────────────────────


def test_point_in_region_smallest_wins():
    big = _region(
        id="big",
        north=LAT + 20 / _M_LAT,
        south=LAT - 20 / _M_LAT,
        east=LON + 20 / (_M_LAT * math.cos(math.radians(LAT))),
        west=LON - 20 / (_M_LAT * math.cos(math.radians(LAT))),
    )
    small = _region(id="small", zone_entity_id="switch.back_yard_grass_green")
    regions = [big, small]
    # dead centre is inside both -> the smaller one answers
    assert region_at_point(regions, 0.5, 0.5, FRAME).id == "small"
    # a corner of the big one only
    assert region_at_point(regions, 0.5, 0.25, FRAME).id == "big"
    # outside everything
    assert region_at_point(regions, 0.99, 0.99, FRAME) is None
    assert region_at_point([], 0.5, 0.5, FRAME) is None
