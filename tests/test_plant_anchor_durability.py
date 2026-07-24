"""Durable plant anchors (v1.59).

Before v1.59 a placement was stored ONLY as map_x/map_y — a fraction of whatever
aerial frame happened to be current. Re-fetching at a different centre or zoom
re-projected them, and anything landing outside the new frame had its position
NULLED. One zoom-in (or a refresh that snapped back to the HA home) silently
erased every marker with no undo. These lock in the replacement contract:

  the ground anchor is the truth; the frame is just a window onto it.
"""

from __future__ import annotations

from dataclasses import replace

from custom_components.complete_irrigation.plant import PlantRecord, PlantStore
from custom_components.complete_irrigation.yard_map import (
    bbox_from_center,
    latlon_to_norm_raw,
    norm_to_latlon,
)

LAT, LON = 33.30, -111.80  # a generic point near Chandler, AZ — not a real address
FRAME = bbox_from_center(LAT, LON, 60.0)


def _plant(**kw) -> PlantRecord:
    base = {
        "id": "p1",
        "name": "Lemon Tree",
        "wucols_category": "moderate",
        "canopy_area_sqft": 40.0,
        "zone_entity_id": "switch.zone_a",
    }
    base.update(kw)
    return PlantRecord(**base)


# ── the record carries the anchor ───────────────────────────────────


def test_anchor_defaults_to_none_and_round_trips():
    p = _plant()
    assert p.anchor_lat is None and p.anchor_lon is None
    p2 = _plant(map_x=0.5, map_y=0.5, anchor_lat=LAT, anchor_lon=LON)
    back = PlantRecord.from_dict(p2.to_dict())
    assert back.anchor_lat == LAT
    assert back.anchor_lon == LON


def test_legacy_record_without_anchor_still_loads():
    # A pre-v1.59 record has no anchor keys at all — it must not fail to load.
    d = _plant(map_x=0.25, map_y=0.75).to_dict()
    d.pop("anchor_lat", None)
    d.pop("anchor_lon", None)
    back = PlantRecord.from_dict(d)
    assert back.map_x == 0.25
    assert back.anchor_lat is None


def test_junk_anchor_degrades_to_none_rather_than_dropping_the_plant():
    for bad in ("abc", float("nan"), float("inf"), 91.0, -91.0):
        d = _plant(map_x=0.5, map_y=0.5).to_dict()
        d["anchor_lat"] = bad
        d["anchor_lon"] = LON
        back = PlantRecord.from_dict(d)
        assert back.anchor_lat is None, bad
        assert back.name == "Lemon Tree"  # record survives


# ── backfill migration ──────────────────────────────────────────────


def test_backfill_gives_existing_placements_an_anchor():
    store = PlantStore()
    store.add(_plant(id="a", map_x=0.25, map_y=0.75))
    store.add(_plant(id="b", map_x=0.5, map_y=0.5))
    store.add(_plant(id="c"))  # never placed
    assert store.backfill_anchors(FRAME) == 2
    a = store.get("a")
    assert a.anchor_lat is not None and a.anchor_lon is not None
    # The anchor must describe the SAME point the marker sat on (to well under
    # a millimetre of ground — the float round-trip through lat/lon is not bit-exact).
    rx, ry = latlon_to_norm_raw(a.anchor_lat, a.anchor_lon, FRAME)
    assert abs(rx - 0.25) < 1e-9
    assert abs(ry - 0.75) < 1e-9
    assert store.get("c").anchor_lat is None  # unplaced stays unplaced


def test_backfill_is_idempotent_and_never_overwrites():
    store = PlantStore()
    store.add(_plant(id="a", map_x=0.25, map_y=0.75, anchor_lat=1.0, anchor_lon=2.0))
    assert store.backfill_anchors(FRAME) == 0
    assert store.get("a").anchor_lat == 1.0  # untouched
    store.add(_plant(id="b", map_x=0.4, map_y=0.4))
    assert store.backfill_anchors(FRAME) == 1
    assert store.backfill_anchors(FRAME) == 0  # second pass is a no-op


def test_backfill_without_a_bbox_is_a_no_op():
    store = PlantStore()
    store.add(_plant(id="a", map_x=0.25, map_y=0.75))
    assert store.backfill_anchors(None) == 0


# ── the durability contract ─────────────────────────────────────────


def _reproject(plant: PlantRecord, new_bbox) -> PlantRecord:
    """The v1.59 re-projection rule used by set_yard_map, in miniature."""
    nx, ny = latlon_to_norm_raw(plant.anchor_lat, plant.anchor_lon, new_bbox)
    inside = 0.0 <= nx <= 1.0 and 0.0 <= ny <= 1.0
    return replace(plant, map_x=nx if inside else None, map_y=ny if inside else None)


def test_marker_keeps_its_ground_position_when_the_frame_moves():
    p = _plant(map_x=0.5, map_y=0.5)
    lat, lon = norm_to_latlon(0.5, 0.5, FRAME)
    p = replace(p, anchor_lat=lat, anchor_lon=lon)
    # Shift the frame 10 m north: the plant must move DOWN the image, not jump.
    moved_frame = bbox_from_center(LAT + 10 / 111_320.0, LON, 60.0)
    p2 = _reproject(p, moved_frame)
    assert p2.map_y > 0.5  # further from the north edge
    assert abs(p2.map_x - 0.5) < 1e-9
    # And it still describes the same ground.
    assert (
        norm_to_latlon(p2.map_x, p2.map_y, moved_frame)
        == (
            p.anchor_lat,
            p.anchor_lon,
        )
        or abs(norm_to_latlon(p2.map_x, p2.map_y, moved_frame)[0] - p.anchor_lat) < 1e-9
    )


def test_zooming_past_a_plant_hides_it_but_keeps_the_anchor():
    # THE regression this release exists for.
    p = _plant(map_x=0.95, map_y=0.95)
    lat, lon = norm_to_latlon(0.95, 0.95, FRAME)
    p = replace(p, anchor_lat=lat, anchor_lon=lon)
    tight = bbox_from_center(LAT, LON, 20.0)  # zoom in past it
    hidden = _reproject(p, tight)
    assert hidden.map_x is None and hidden.map_y is None  # not drawn
    assert hidden.anchor_lat == lat and hidden.anchor_lon == lon  # NOT lost
    # Zoom back out — the marker returns to exactly where it was.
    restored = _reproject(hidden, FRAME)
    assert abs(restored.map_x - 0.95) < 1e-9
    assert abs(restored.map_y - 0.95) < 1e-9


def test_a_round_trip_through_several_frames_does_not_drift():
    p = _plant(map_x=0.3, map_y=0.8)
    lat, lon = norm_to_latlon(0.3, 0.8, FRAME)
    p = replace(p, anchor_lat=lat, anchor_lon=lon)
    for span in (20.0, 500.0, 35.0, 120.0):
        p = _reproject(p, bbox_from_center(LAT, LON, span))
    p = _reproject(p, FRAME)
    assert abs(p.map_x - 0.3) < 1e-9
    assert abs(p.map_y - 0.8) < 1e-9


def test_rotation_does_not_touch_anchors_at_all():
    # Rotation is a display transform: it changes no stored coordinate.
    from custom_components.complete_irrigation.yard_map import normalize_rotation

    p = _plant(map_x=0.42, map_y=0.17, anchor_lat=LAT, anchor_lon=LON)
    before = p.to_dict()
    normalize_rotation(37)  # whatever the user spins to
    assert p.to_dict() == before
