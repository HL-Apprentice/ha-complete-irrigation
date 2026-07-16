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
from typing import Any

# Metres per degree of latitude (mean); longitude scales by cos(latitude).
_M_PER_DEG_LAT = 111_320.0
# Esri World Imagery static export — public, no API key required.
_ESRI_EXPORT = (
    "https://services.arcgisonline.com/arcgis/rest/services/World_Imagery/MapServer/export"
)
DEFAULT_SPAN_M = 60.0  # a typical residential lot is ~tens of metres across
MAX_IMAGE_PX = 1536  # cap the longer image edge (keeps the fetch small + sharp)
# Esri's export refuses to render sharper than its imagery cache (observed 2026-07:
# HTTP 500 "Error: bytes" below ~0.24 m/px; ~level-19 cache is ~0.3 m/px). Fetch at
# or above this scale and UPSCALE locally for display — same bbox, so marker math
# is untouched. 0.3 keeps a safety margin over the measured 0.25-works/0.22-fails cliff.
MIN_EXPORT_M_PER_PX = 0.3


def safe_export_size(span_m: float, width: int, height: int) -> tuple[int, int]:
    """Shrink a requested (width, height) so the export stays at or above
    MIN_EXPORT_M_PER_PX for a span_m-wide bbox (Esri 500s on anything sharper).
    Aspect is preserved; returns the original size when it's already coarse enough."""
    longer = max(width, height)
    if longer <= 0 or span_m <= 0:
        return (width, height)
    max_px = int(span_m / MIN_EXPORT_M_PER_PX)
    if longer <= max_px:
        return (width, height)
    scale = max_px / longer
    return (max(1, round(width * scale)), max(1, round(height * scale)))


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


# v1.42 — a CUSTOM aerial source. Esri's global World Imagery is coarse at yard
# scale (it refuses to render sharper than ~0.3 m/px, so a 60 m yard is fetched
# at ~200 px and upscaled — soft and blocky). Many county assessors/GIS offices
# publish far sharper orthophotos as a keyless ArcGIS MapServer `export`. Rather
# than hardcode any one county, the user supplies a URL TEMPLATE.
#
# Substitution is plain str.replace, NOT str.format: a user-supplied format
# string could reach attributes ("{width.__class__…}") — replace has no such
# surface, and an unknown "{foo}" is simply left alone instead of raising.
_TEMPLATE_TOKENS = ("{bbox}", "{west}", "{south}", "{east}", "{north}", "{width}", "{height}")


def format_export_template(template: str, bbox: Bbox, width: int, height: int) -> str:
    """Fill an export URL template. Supported tokens: {bbox} (w,s,e,n), the four
    edges individually ({west}/{south}/{east}/{north}), {width}, {height}."""
    w, s, e, n = bbox.as_tuple()
    out = template
    for token, value in (
        ("{bbox}", f"{w:.8f},{s:.8f},{e:.8f},{n:.8f}"),
        ("{west}", f"{w:.8f}"),
        ("{south}", f"{s:.8f}"),
        ("{east}", f"{e:.8f}"),
        ("{north}", f"{n:.8f}"),
        ("{width}", str(int(width))),
        ("{height}", str(int(height))),
    ):
        out = out.replace(token, value)
    return out


def valid_export_template(template: Any) -> bool:
    """True when a template is usable: http(s), places the bbox (either {bbox} or
    all four edges), and carries both {width} and {height}. Anything else would
    silently fetch the wrong ground area — reject it at the config boundary."""
    if not isinstance(template, str) or not template.strip():
        return False
    t = template.strip()
    if not t.startswith(("http://", "https://")):
        return False
    has_bbox = "{bbox}" in t or all(k in t for k in ("{west}", "{south}", "{east}", "{north}"))
    return has_bbox and "{width}" in t and "{height}" in t


def export_url(bbox: Bbox, width: int, height: int, template: str | None = None) -> str:
    """The aerial export URL: the user's template when set, else keyless Esri."""
    if template and template.strip():
        return format_export_template(template.strip(), bbox, width, height)
    return esri_export_url(bbox, width, height)


def norm_from_yard_map(lat, lon, yard_map_cfg) -> tuple[float, float] | None:
    """v1.32 — (map_x, map_y) for a GPS coordinate using a stored yard-map config's
    bbox, or None if the config / bbox / coordinate is missing or invalid. This is
    the bridge a photo's EXIF GPS uses to auto-place a plant marker. The result is
    only a GUESS (phone GPS is ~3-5 m); the user can drag to correct."""
    if not isinstance(yard_map_cfg, dict):
        return None
    bb = yard_map_cfg.get("bbox")
    if not isinstance(bb, dict):
        return None
    try:
        bbox = Bbox(float(bb["west"]), float(bb["south"]), float(bb["east"]), float(bb["north"]))
        return latlon_to_norm(float(lat), float(lon), bbox)
    except (KeyError, TypeError, ValueError):
        return None
