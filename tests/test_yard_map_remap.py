"""Marker re-projection when the yard map's zoom / centre changes (v1.43).

Markers are stored normalized to the bbox that was current when they were placed,
so changing the span must round-trip them through lat/lon or every plant slides
across the yard.
"""

from __future__ import annotations

import pytest

from custom_components.complete_irrigation.yard_map import (
    Bbox,
    bbox_from_center,
    bbox_from_cfg,
    latlon_to_norm,
    latlon_to_norm_raw,
    norm_to_latlon,
    remap_norm,
)

LAT, LON = 33.2272298, -111.7257696
WIDE = bbox_from_center(LAT, LON, 60.0)
TIGHT = bbox_from_center(LAT, LON, 30.0)


def test_centre_marker_stays_centred_when_zooming():
    # The centre of the old view is the centre of the new one.
    assert remap_norm(0.5, 0.5, WIDE, TIGHT) == (0.5, 0.5)


def test_real_world_position_is_preserved():
    # A point 10 m east/north of centre must land on the SAME ground spot after a
    # zoom, i.e. its lat/lon round-trips.
    x, y = latlon_to_norm(LAT + 0.00009, LON + 0.00010, WIDE)
    remapped = remap_norm(x, y, WIDE, TIGHT)
    assert remapped is not None
    before = norm_to_latlon(x, y, WIDE)
    after = norm_to_latlon(*remapped, TIGHT)
    assert abs(before[0] - after[0]) < 1e-7
    assert abs(before[1] - after[1]) < 1e-7


def test_zooming_in_drops_a_marker_outside_the_new_view():
    # A corner of the 60 m view is well outside the 30 m view -> un-place, not clamp.
    assert remap_norm(0.02, 0.02, WIDE, TIGHT) is None


def test_zooming_out_keeps_everything():
    for xy in ((0.0, 0.0), (0.5, 0.5), (1.0, 1.0), (0.13, 0.87)):
        assert remap_norm(*xy, TIGHT, WIDE) is not None


def test_dropped_marker_is_not_silently_clamped_to_the_edge():
    # The bug this guards: clamping would pin an out-of-view plant to the border
    # and assert it's there. None is the honest answer.
    out = remap_norm(0.0, 0.0, WIDE, TIGHT)
    assert out is None
    assert out != (0.0, 0.0)


def test_identical_bbox_is_a_no_op():
    # Round-tripping through lat/lon costs a few float ULPs; that's fine, it just
    # must not MOVE the marker.
    out = remap_norm(0.31, 0.62, WIDE, WIDE)
    assert out is not None
    assert out[0] == pytest.approx(0.31, abs=1e-9)
    assert out[1] == pytest.approx(0.62, abs=1e-9)


def test_raw_norm_is_unclamped_but_clamped_variant_is_not():
    far_lat, far_lon = LAT + 0.01, LON + 0.01  # ~1 km away, far outside
    rx, ry = latlon_to_norm_raw(far_lat, far_lon, WIDE)
    assert rx > 1.0 or ry < 0.0  # escaped the box
    cx, cy = latlon_to_norm(far_lat, far_lon, WIDE)
    assert 0.0 <= cx <= 1.0 and 0.0 <= cy <= 1.0  # clamped


def test_raw_norm_degenerate_bbox():
    degenerate = Bbox(1.0, 1.0, 1.0, 1.0)
    assert latlon_to_norm_raw(1.0, 1.0, degenerate) == (0.5, 0.5)


# ── bbox_from_cfg ───────────────────────────────────────────────────


def test_bbox_from_cfg_roundtrip():
    cfg = {
        "bbox": {
            "west": WIDE.west,
            "south": WIDE.south,
            "east": WIDE.east,
            "north": WIDE.north,
        }
    }
    assert bbox_from_cfg(cfg) == WIDE


def test_bbox_from_cfg_rejects_malformed():
    assert bbox_from_cfg(None) is None
    assert bbox_from_cfg({}) is None
    assert bbox_from_cfg({"bbox": "nope"}) is None
    assert bbox_from_cfg({"bbox": {"west": 1.0}}) is None  # missing edges
    assert bbox_from_cfg({"bbox": {"west": "x", "south": 1, "east": 2, "north": 3}}) is None
