"""Zone regions (v1.60) — "which loop drives this patch of ground?".

A ZoneRegion is a rectangle of REAL GROUND (four WGS84 degrees) tagged with the
zone/loop entity that waters it. It answers the one question the plant markers
cannot: a lawn has no plant records at all, so nothing on the map keys the grass
zones — draw the grass, name its loop, and the map can tell you.

Ground truth is the ONLY thing persisted. Where the region falls on the current
aerial is derived per-render from that frame's bbox, so moving, zooming, nudging
or rotating the aerial cannot displace, shrink or lose a region: there is one
representation, and it is the one that cannot go stale. (Plants still carry both
a normalized position and an anchor; that dual truth is why a re-fetch could
once erase them. Regions do not repeat it.)

Pure + HA-free, so it is unit-testable like plant.py / yard_map.py.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any

from .yard_map import Bbox, latlon_to_norm_raw

_LOGGER = logging.getLogger(__name__)

# Same safe-slug rule as plant ids. A region id never names a filesystem path,
# but keeping one id grammar across the integration means one thing to reason
# about, and it keeps the value safe to interpolate into a CSS/DOM selector.
_SAFE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")

# A well-formed HA entity id. Deliberately NOT a check that the entity exists:
# add_plant / add_schedule accept any well-formed id because a cloud zone is
# legitimately absent during a restore or before its integration has loaded, and
# hard-failing there would break backup restores.
_ENTITY_ID_RE = re.compile(r"[a-z0-9_]+\.[a-z0-9_]+")

_MAX_LABEL = 60
_MAX_REGIONS = 200  # a yard has far fewer; bounds the yard_report payload
_MIN_SIDE_M = 0.5  # a narrower band is a mis-drag, not a bed
_MAX_AREA_SQFT = 200_000.0  # ~4.6 acres — well past any residential yard
_M_PER_DEG_LAT = 111_320.0
_SQFT_PER_SQM = 10.7639
SCHEMA = 1


def _finite(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


@dataclass(frozen=True)
class ZoneRegion:
    """A ground rectangle tagged with the loop that waters it."""

    id: str
    zone_entity_id: str
    north: float
    south: float
    east: float
    west: float
    label: str = ""
    schema: int = field(default=SCHEMA)

    def __post_init__(self) -> None:
        if not _SAFE_ID_RE.fullmatch(self.id or ""):
            raise ValueError(f"region id must be 1-64 chars of [A-Za-z0-9_-], got {self.id!r}")
        if not _ENTITY_ID_RE.fullmatch(self.zone_entity_id or ""):
            raise ValueError(
                f"zone_entity_id must be a valid entity id, got {self.zone_entity_id!r}"
            )
        if not isinstance(self.label, str) or len(self.label) > _MAX_LABEL:
            raise ValueError(f"label must be a string of at most {_MAX_LABEL} chars")
        if any(ord(c) < 32 or ord(c) == 127 for c in self.label):
            raise ValueError("label must not contain control characters")
        for name, val, limit in (
            ("north", self.north, 90.0),
            ("south", self.south, 90.0),
            ("east", self.east, 180.0),
            ("west", self.west, 180.0),
        ):
            f = _finite(val)
            if f is None or not (-limit <= f <= limit):
                raise ValueError(f"{name} must be a finite degree value in [-{limit}, {limit}]")
        # Canonical corner order, fixed at construction so no caller has to know
        # which way round they passed them (a drag can end north-west of where it
        # started). Frozen dataclass -> object.__setattr__.
        n, s = float(self.north), float(self.south)
        e, w = float(self.east), float(self.west)
        # Read BOTH before writing either — assigning north first would clobber
        # the value south still needs, collapsing the pair instead of swapping.
        object.__setattr__(self, "north", max(n, s))
        object.__setattr__(self, "south", min(n, s))
        object.__setattr__(self, "east", max(e, w))
        object.__setattr__(self, "west", min(e, w))
        h_m, w_m = self.height_m, self.width_m
        if h_m < _MIN_SIDE_M or w_m < _MIN_SIDE_M:
            raise ValueError(
                f"region must be at least {_MIN_SIDE_M} m on each side, got {w_m:.2f} x {h_m:.2f} m"
            )
        if self.area_sqft > _MAX_AREA_SQFT:
            raise ValueError(f"region area must be at most {_MAX_AREA_SQFT:.0f} sq ft")

    # ── ground measurements ─────────────────────────────────────────
    @property
    def height_m(self) -> float:
        return abs(self.north - self.south) * _M_PER_DEG_LAT

    @property
    def width_m(self) -> float:
        mid_lat = (self.north + self.south) / 2.0
        return abs(self.east - self.west) * _M_PER_DEG_LAT * math.cos(math.radians(mid_lat))

    @property
    def area_sqft(self) -> float:
        """Ground area, the FULL rectangle (a lawn is not the pi/4 ellipse a
        canopy is). Advisory/reporting only — nothing in the run path reads it."""
        return round(self.height_m * self.width_m * _SQFT_PER_SQM, 1)

    # ── serialization ───────────────────────────────────────────────
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "zone_entity_id": self.zone_entity_id,
            "north": self.north,
            "south": self.south,
            "east": self.east,
            "west": self.west,
            "label": self.label,
            "schema": SCHEMA,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ZoneRegion:
        # An unknown schema is SKIPPED by the caller rather than read under v1
        # key meanings — the only forward-compat escape hatch we have, since the
        # HA Store version stays 1 and tolerant reads only cover added fields.
        raw_schema = data.get("schema", SCHEMA)
        if int(raw_schema) != SCHEMA:
            raise ValueError(f"unsupported region schema {raw_schema!r}")
        return cls(
            id=str(data["id"]),
            zone_entity_id=str(data["zone_entity_id"]),
            north=data["north"],
            south=data["south"],
            east=data["east"],
            west=data["west"],
            label=str(data.get("label") or ""),
        )


def derive_box(region: ZoneRegion, bbox: Bbox) -> tuple[float, float, float, float]:
    """Where the region falls on the CURRENT frame: (x0, y0, x1, y1) normalized.

    Deliberately UNCLAMPED. A grass loop routinely runs past the edge of the
    aerial, and clamping would silently redraw it as a smaller rectangle glued to
    the border — a confident lie about which ground the loop covers. The panel
    clips visually (the map wrapper is overflow:hidden); the numbers stay honest.
    """
    x0, y0 = latlon_to_norm_raw(region.north, region.west, bbox)  # NW corner
    x1, y1 = latlon_to_norm_raw(region.south, region.east, bbox)  # SE corner
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def box_intersects_frame(box: tuple[float, float, float, float]) -> bool:
    """True if any of the region is on screen (else the panel draws nothing)."""
    x0, y0, x1, y1 = box
    return x1 >= 0.0 and x0 <= 1.0 and y1 >= 0.0 and y0 <= 1.0


def region_at_point(regions: list[ZoneRegion], x: float, y: float, bbox: Bbox) -> ZoneRegion | None:
    """The region under a normalized point — SMALLEST containing one wins.

    Without the smallest-wins rule a whole-back-yard region would swallow every
    tap, making anything drawn inside it unreachable. Ties (identical area) are
    broken by id so the answer is deterministic.
    """
    hits = []
    for r in regions:
        x0, y0, x1, y1 = derive_box(r, bbox)
        if x0 <= x <= x1 and y0 <= y <= y1:
            hits.append(((x1 - x0) * (y1 - y0), r.id, r))
    if not hits:
        return None
    hits.sort(key=lambda t: (t[0], t[1]))
    return hits[0][2]


class ZoneRegionStore:
    """In-memory dict of ZoneRegion by id — same shape as PlantStore."""

    def __init__(self) -> None:
        self._items: dict[str, ZoneRegion] = {}

    def add(self, region: ZoneRegion) -> None:
        if region.id in self._items:
            raise KeyError(f"region with id {region.id!r} already exists")
        self._items[region.id] = region

    def upsert(self, region: ZoneRegion) -> None:
        self._items[region.id] = region

    def get(self, region_id: str) -> ZoneRegion | None:
        return self._items.get(region_id)

    def delete(self, region_id: str) -> bool:
        return self._items.pop(region_id, None) is not None

    def all(self) -> list[ZoneRegion]:
        return list(self._items.values())

    def by_zone(self, zone_entity_id: str) -> list[ZoneRegion]:
        return [r for r in self._items.values() if r.zone_entity_id == zone_entity_id]

    def to_serializable(self) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self._items.values()]

    @classmethod
    def from_serializable(cls, data: Any) -> ZoneRegionStore:
        """Load, skipping (and logging) any record that fails validation.

        One corrupt row must never take down the whole yard view, so this mirrors
        PlantStore.from_serializable rather than raising.
        """
        store = cls()
        if not isinstance(data, list):
            return store
        for raw in data:
            if len(store._items) >= _MAX_REGIONS:
                _LOGGER.warning(
                    "zone regions: more than %d stored; ignoring the rest", _MAX_REGIONS
                )
                break
            if not isinstance(raw, dict):
                continue
            try:
                region = ZoneRegion.from_dict(raw)
            except (KeyError, TypeError, ValueError) as err:
                _LOGGER.warning("zone regions: skipping unreadable record (%s)", err)
                continue
            store._items[region.id] = region
        return store
