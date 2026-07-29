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

import ipaddress
import math
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

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


def latlon_to_norm_raw(lat: float, lon: float, bbox: Bbox) -> tuple[float, float]:
    """Like latlon_to_norm but UNCLAMPED, so a caller can tell an inside-the-view
    point from one that merely clamps to the edge. Needed by the re-projection in
    plant.reproject_plants: clamping
    first would silently pile every out-of-view marker onto the border."""
    ew = bbox.east - bbox.west
    ns = bbox.north - bbox.south
    if ew <= 0 or ns <= 0:
        return (0.5, 0.5)
    return ((lon - bbox.west) / ew, (bbox.north - lat) / ns)


def norm_to_latlon(x: float, y: float, bbox: Bbox) -> tuple[float, float]:
    """Normalized (x, y) -> (lat, lon). Inverse of latlon_to_norm."""
    lon = bbox.west + _clamp01(x) * (bbox.east - bbox.west)
    lat = bbox.north - _clamp01(y) * (bbox.north - bbox.south)
    return (lat, lon)


# 1 m^2 = 10.7639 ft^2. Lives here because both area helpers below use it.
_SQFT_PER_SQM = 10.7639


def canopy_sqft_from_box(dx_norm: float, dy_norm: float, span_m: float) -> float:
    """Canopy footprint in ft² for a box drawn on the aerial map — a no-LLM way to
    measure canopy straight off the top-down image.

    ``dx_norm`` / ``dy_norm`` are the box width / height as a FRACTION of the map
    (0-1); ``span_m`` is the map's ground span (the bbox is square-ground, so both
    axes scale by span_m from normalized to metres). The canopy is modelled as the
    ellipse inscribed in the box (canopies are round-ish): area = pi/4 * W * H.
    """
    w_m = abs(float(dx_norm)) * float(span_m)
    h_m = abs(float(dy_norm)) * float(span_m)
    area_m2 = math.pi / 4.0 * w_m * h_m
    return round(area_m2 * _SQFT_PER_SQM, 1)


def region_sqft_from_box(dx_norm: float, dy_norm: float, span_m: float) -> float:
    """Ground area in ft² for a REGION box drawn on the aerial (v1.60).

    Same inputs as ``canopy_sqft_from_box`` but the full RECTANGLE, not the
    inscribed ellipse: a lawn or bed covers the area you dragged, whereas a
    canopy is round-ish. Using the canopy formula here would under-report every
    region by ~21% (1 - pi/4) and quietly skew any water math built on it.
    """
    w_m = abs(float(dx_norm)) * float(span_m)
    h_m = abs(float(dy_norm)) * float(span_m)
    return round(w_m * h_m * _SQFT_PER_SQM, 1)


def bbox_from_cfg(cfg: Any) -> Bbox | None:
    """Bbox from a stored yard_map config dict, or None if absent/malformed."""
    if not isinstance(cfg, dict):
        return None
    b = cfg.get("bbox")
    if not isinstance(b, dict):
        return None
    try:
        return Bbox(float(b["west"]), float(b["south"]), float(b["east"]), float(b["north"]))
    except (KeyError, TypeError, ValueError):
        return None


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


def normalize_export_template(template: str) -> str:
    """Undo percent-encoding of the TOKEN BRACES only, and trim.

    `{` and `}` are not legal URL characters (RFC 3986), so anything that treats
    the template as a real URL — a browser address bar, a linkified URL in a chat
    or doc, many "copy link" buttons — hands back `%7Bbbox%7D` instead of
    `{bbox}`. That is the overwhelmingly common way a user obtains this string, so
    accept it rather than rejecting a template that is correct in spirit.

    Deliberately narrow: only %7B/%7D are decoded, NOT the whole URL. A blanket
    unquote would corrupt legitimately-encoded query values (e.g. a %26 that is
    meant to stay data, not become a parameter separator).
    """
    if not isinstance(template, str):
        return ""
    out = template.strip()
    for enc in ("%7B", "%7b"):
        out = out.replace(enc, "{")
    for enc in ("%7D", "%7d"):
        out = out.replace(enc, "}")
    return out


def format_export_template(template: str, bbox: Bbox, width: int, height: int) -> str:
    """Fill an export URL template. Supported tokens: {bbox} (w,s,e,n), the four
    edges individually ({west}/{south}/{east}/{north}), {width}, {height}."""
    w, s, e, n = bbox.as_tuple()
    out = normalize_export_template(template)
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
    # Accept percent-encoded braces — a template copied from a browser/linkified
    # URL arrives as %7Bbbox%7D and is correct in every way that matters.
    t = normalize_export_template(template)
    if not t.startswith(("http://", "https://")):
        return False
    has_bbox = "{bbox}" in t or all(k in t for k in ("{west}", "{south}", "{east}", "{north}"))
    return has_bbox and "{width}" in t and "{height}" in t


def offset_center(lat: float, lon: float, north_m: float, east_m: float) -> tuple[float, float]:
    """v1.58.1 — shift a map center by meters (north positive, east positive).

    Lets the aerial frame be nudged (e.g. "the yard is clipped on the north side
    — shift 3 m north") without touching the configured/HA home coordinates.
    Local flat-earth math, exact at yard scale."""
    lat2 = lat + (north_m or 0.0) / _M_PER_DEG_LAT
    # Longitude meters shrink with latitude; guard the cos for pole edge cases.
    scale = max(0.01, math.cos(math.radians(lat)))
    lon2 = lon + (east_m or 0.0) / (_M_PER_DEG_LAT * scale)
    return lat2, lon2


# Hostnames that must never be an aerial-export target — resolving the HA server
# at these is SSRF, never a real imagery source.
_BLOCKED_EXPORT_HOSTNAMES = frozenset({"localhost", "metadata", "metadata.google.internal"})


def export_host_allowed(url_or_template: Any) -> bool:
    """SSRF guard for the custom aerial-export URL (v1.52.1). Rejects targets that
    are never a legitimate imagery host but ARE dangerous to fetch server-side:
    loopback, link-local (incl. 169.254.169.254 cloud metadata), unspecified,
    multicast, and the localhost/metadata hostnames. RFC-1918 / ULA private ranges
    are allowed on purpose, so a self-hoster can point at their own LAN GIS server.
    A hostname that RESOLVES to a blocked IP (DNS rebinding) is a residual —
    admin-gated and blind (the response must still pass the image-magic-byte sniff)."""
    if not isinstance(url_or_template, str) or not url_or_template.strip():
        return False
    try:
        parsed = urlparse(normalize_export_template(url_or_template.strip()))
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if not host or host in _BLOCKED_EXPORT_HOSTNAMES:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True  # a hostname (not an IP literal) — allowed; see DNS-rebinding note
    return not (ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast)


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


# ── v1.59 aerial rotation ────────────────────────────────────────────
# Esri (and any bbox export) hands back a NORTH-UP, axis-aligned image, so a
# property whose lot sits at an angle to true north reads as crooked. Rotation
# is therefore a pure DISPLAY transform over that image: the bbox, the stored
# normalized marker coords, and every ground position are untouched — only the
# presentation turns. That keeps it lossless and instantly reversible (unlike
# the frame nudge, which re-fetches different ground).


def normalize_rotation(deg) -> float:
    """Wrap a rotation to the half-open range (-180, 180]; junk/None -> 0.0.

    Wrapping (rather than clamping) means repeated ↻ taps never jam at a limit
    and -181 == 179 rather than being rejected.
    """
    try:
        d = float(deg)
    except (TypeError, ValueError):
        return 0.0
    if d != d or d in (float("inf"), float("-inf")):  # NaN / inf
        return 0.0
    d = math.fmod(d, 360.0)
    if d <= -180.0:
        d += 360.0
    elif d > 180.0:
        d -= 360.0
    return d + 0.0  # normalize -0.0 -> 0.0


def cover_scale(width: float, height: float, deg) -> float:
    """Scale factor so a width x height image rotated by ``deg`` still covers its
    own width x height frame (no empty triangles at the corners).

    Rotating the FRAME by -deg gives an axis-aligned box of
    (W|cos| + H|sin|) x (W|sin| + H|cos|); requiring that to fit inside the
    scaled image on both axes reduces to |cos| + max(W/H, H/W) * |sin|.
    """
    try:
        w = float(width)
        h = float(height)
    except (TypeError, ValueError):
        return 1.0
    if not (w > 0 and h > 0):
        return 1.0
    r = math.radians(normalize_rotation(deg))
    c, s = abs(math.cos(r)), abs(math.sin(r))
    return c + max(w / h, h / w) * s
