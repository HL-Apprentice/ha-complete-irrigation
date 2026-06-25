"""Tests for the yard-map georeferencing core (pure logic, v1.30).

Covers bbox-from-center, the lat/lon <-> normalized-xy mapping (exact + linear at
yard scale), aspect-correct image sizing, and the keyless Esri export URL. This is
the math both manual marker placement and future EXIF-GPS auto-placement reuse.
"""

from __future__ import annotations

from custom_components.complete_irrigation.yard_map import (
    Bbox,
    bbox_from_center,
    esri_export_url,
    image_size_for_bbox,
    latlon_to_norm,
    norm_to_latlon,
)

LAT, LON = 33.4, -111.8  # a Phoenix-area yard


def test_bbox_centered_and_square_ground_area():
    bb = bbox_from_center(LAT, LON, span_m=60)
    # Centre of the box is the input point.
    assert (bb.west + bb.east) / 2 == LON
    assert (bb.south + bb.north) / 2 == LAT
    # Longitude span is WIDER in degrees than latitude (lon degrees shrink with
    # cos(lat)) so the GROUND area stays square.
    assert (bb.east - bb.west) > (bb.north - bb.south)


def test_center_maps_to_middle():
    bb = bbox_from_center(LAT, LON, span_m=60)
    x, y = latlon_to_norm(LAT, LON, bb)
    assert abs(x - 0.5) < 1e-9
    assert abs(y - 0.5) < 1e-9


def test_corners_map_to_unit_box():
    bb = bbox_from_center(LAT, LON, span_m=60)
    # NW corner (north lat, west lon) -> (0, 0); SE corner -> (1, 1).
    assert latlon_to_norm(bb.north, bb.west, bb) == (0.0, 0.0)
    assert latlon_to_norm(bb.south, bb.east, bb) == (1.0, 1.0)


def test_latlon_norm_clamps_outside_box():
    bb = bbox_from_center(LAT, LON, span_m=60)
    # A point well north-west of the box clamps into [0, 1].
    x, y = latlon_to_norm(bb.north + 1, bb.west - 1, bb)
    assert 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0
    assert x == 0.0 and y == 0.0


def test_norm_latlon_round_trip():
    bb = bbox_from_center(LAT, LON, span_m=80)
    for nx, ny in ((0.25, 0.75), (0.5, 0.5), (0.1, 0.9)):
        lat, lon = norm_to_latlon(nx, ny, bb)
        rx, ry = latlon_to_norm(lat, lon, bb)
        assert abs(rx - nx) < 1e-9 and abs(ry - ny) < 1e-9


def test_image_size_preserves_aspect():
    bb = bbox_from_center(LAT, LON, span_m=60)
    w, h = image_size_for_bbox(bb, max_px=1536)
    assert max(w, h) == 1536
    # Wider than tall, matching the degree aspect (so the square ground isn't stretched).
    assert w == 1536 and h < w
    assert abs((w / h) - ((bb.east - bb.west) / (bb.north - bb.south))) < 0.01


def test_esri_url_well_formed():
    bb = Bbox(-111.81, 33.39, -111.79, 33.41)
    url = esri_export_url(bb, 1536, 1282)
    assert url.startswith("https://services.arcgisonline.com/")
    assert "World_Imagery" in url
    assert "bboxSR=4326" in url and "imageSR=4326" in url
    assert "size=1536,1282" in url
    assert "f=image" in url


def test_degenerate_bbox_is_safe():
    # A zero-area bbox must not divide by zero — defaults to centre / square.
    bb = Bbox(0.0, 0.0, 0.0, 0.0)
    assert latlon_to_norm(0.0, 0.0, bb) == (0.5, 0.5)
    assert image_size_for_bbox(bb) == (1536, 1536)
