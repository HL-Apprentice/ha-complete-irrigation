"""Yard-map georeferencing — pure logic, HA-free (like hydraulics / et_calc).

The yard map is an aerial image of the property with draggable plant markers.
Plant positions are stored as NORMALIZED coordinates (map_x, map_y in [0, 1] —
fraction from the west / north edge), so they're independent of the rendered
pixel size. This module is the bridge between geographic coordinates and those
normalized positions:

  * `bbox_from_center` — a square-ground-area bounding box around the HA latitude
    / longitude, for requesting the aerial.
  * `latlon_to_norm` / `norm_to_latlon` — convert between a real GPS coordinate and
    the normalized map position. `latlon_to_norm` is what EXIF-GPS auto-placement
    (Phase 3) will use to drop a marker from a photo's embedded coordinates.
  * `esri_export_url` — the keyless Esri World Imagery static-export URL for a bbox.

At yard scale (tens of metres) the earth is locally flat, so a plate-carrée
(imageSR=4326) image maps linearly in lon/lat — the normalized conversion is exact.
A Bbox is (west, south, east, north) in WGS84 degrees.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Metres per degree of latitude (mean); longitude scales by cos(latitude).
_M_PER_DEG_LAT = 111_320.0
# Esri World Imagery static export — public, no API key required.
_ESRI_EXPORT = (
    "https://services.arcgisonline.com/arcgis/rest/services/World_Imagery/MapServer/export"
)
DEFAULT_SPAN_M = 60.0  # a typical residential lot is ~tens of metres across
MAX_IMAGE_PX = 1536  # cap the longer image edge (keeps the fetch small + sharp)


@dataclass(frozen=True)
class Bbox:
    """A WGS84 bounding box. west/east are longitudes, south/north latitudes."""

    west: float
    south: float
    east: float
    north: float

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.west, self.south, self.east, self.north)


def bbox_from_center(lat: float, lon: float, span_m: float = DEFAULT_SPAN_M) -> Bbox:
    """Square-ground-area bbox (span_m on a side) centred on (lat, lon). The bbox is
    wider in LONGITUDE degrees than latitude (lon degrees shrink with cos(lat)), so the
    ground area stays square; request the image at the matching aspect to avoid stretch."""
    half = max(1.0, span_m) / 2.0
    dlat = half / _M_PER_DEG_LAT
    dlon = half / (_M_PER_DEG_LAT * max(math.cos(math.radians(lat)), 1e-6))
    return Bbox(lon - dlon, lat - dlat, lon + dlon, lat + dlat)


def _clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else 1.0 if v > 1.0 else v


def latlon_to_norm(lat: float, lon: float, bbox: Bbox) -> tuple[float, float]:
    """(lat, lon) -> normalized (x, y) in [0, 1]. x grows EAST from the west edge;
    y grows SOUTH from the north edge (image rows go top-down). Clamped to the box."""
    ew = bbox.east - bbox.west
    ns = bbox.north - bbox.south
    if ew <= 0 or ns <= 0:
        return (0.5, 0.5)
    x = (lon - bbox.west) / ew
    y = (bbox.north - lat) / ns
    return (_clamp01(x), _clamp01(y))


def norm_to_latlon(x: float, y: float, bbox: Bbox) -> tuple[float, float]:
    """Normalized (x, y) -> (lat, lon). Inverse of latlon_to_norm."""
    lon = bbox.west + _clamp01(x) * (bbox.east - bbox.west)
    lat = bbox.north - _clamp01(y) * (bbox.north - bbox.south)
    return (lat, lon)


def image_size_for_bbox(bbox: Bbox, max_px: int = MAX_IMAGE_PX) -> tuple[int, int]:
    """(width, height) px preserving the bbox's degree aspect ratio, longer edge = max_px.
    Matching the aspect keeps the plate-carrée image undistorted."""
    ew = bbox.east - bbox.west
    ns = bbox.north - bbox.south
    if ew <= 0 or ns <= 0:
        return (max_px, max_px)
    if ew >= ns:
        return (max_px, max(1, round(max_px * ns / ew)))
    return (max(1, round(max_px * ew / ns)), max_px)


def esri_export_url(bbox: Bbox, width: int, height: int) -> str:
    """Keyless Esri World Imagery static-export URL for the bbox at the given pixel
    size. imageSR=4326 keeps the image plate-carrée so lon/lat map linearly to x/y."""
    w, s, e, n = bbox.as_tuple()
    return (
        f"{_ESRI_EXPORT}?bbox={w:.8f},{s:.8f},{e:.8f},{n:.8f}"
        f"&bboxSR=4326&imageSR=4326&size={width},{height}&format=jpg&f=image"
    )
