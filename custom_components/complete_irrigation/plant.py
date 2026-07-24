"""Plant records + store (v2 plant-aware irrigation).

A PlantRecord is a persisted entity: a named plant of a known WUCOLS
water-use category, a canopy area, attached to a zone (the physical drip
"loop" = the switch entity a schedule runs). PlantStore is the in-memory
collection with serialization helpers — same shape as ScheduleStore, so
the HA Store wiring that persists schedules persists plants the same way.

Pure + HA-free so it's unit-testable. The hydraulics core consumes these
via `to_calc_plant()`; the panel/services will mutate them later.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, replace
from typing import Any

from .hydraulics import WUCOLS_FACTORS, Plant

_LOGGER = logging.getLogger(__name__)

# Upper sanity bound on canopy area, mirroring the service schema so the
# .storage load path can't accept absurd / non-finite values (a corrupt or
# crafted store would otherwise poison the water math + WS JSON).
_MAX_CANOPY_AREA_SQFT = 100000

# A plant id is used as a FILESYSTEM PATH SEGMENT for its photo directory
# (www/complete_irrigation/plants/<id>/). Constrain it to a safe slug so a
# corrupt / crafted .storage record can never carry "../", a slash, or an
# absolute path into add_plant_photo's file write (path-traversal sink). Our
# own ids are uuid4().hex[:12] = [0-9a-f]{12}, which always passes; the guard
# only rejects hand-tampered values, dropping them on load (from_serializable
# skips records that fail validation) rather than letting them escape the dir.
_SAFE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


@dataclass(frozen=True)
class PlantRecord:
    """One plant in the yard, attached to a zone/loop."""

    id: str
    name: str
    wucols_category: str
    canopy_area_sqft: float
    zone_entity_id: str
    # v1.30 — normalized position on the yard map (fraction from the west / north
    # edge, each in [0, 1]). None = not placed yet. Independent of pixel size.
    map_x: float | None = None
    map_y: float | None = None
    # v1.59 — the plant's GROUND anchor (WGS84), captured when it is placed.
    # map_x/map_y are only where it falls in the CURRENT frame, so they are a
    # derived cache: re-fetching the aerial at a different centre/zoom used to
    # null them, silently losing every placement. The anchor is the durable
    # truth — the frame can move, zoom or rotate and the plant stays put; a
    # plant outside the current view just isn't drawn until the view covers it
    # again. None on records placed before v1.59 (backfilled from the stored
    # bbox on first load, see PlantStore.backfill_anchors).
    anchor_lat: float | None = None
    anchor_lon: float | None = None
    # v1.54 — free-text light-AREA label grouping co-located plants (e.g. "Front
    # Bed"). Plants sharing an area are surveyed together (one lux reading applies
    # to all). "" = ungrouped. Assigned per-plant or by drawing a region on the
    # yard map (plants_in_region maps a drawn box -> the enclosed plant ids).
    area: str = ""
    # v1.32 — photo history for biannual LLM-vision health checks. Each entry:
    # {"ts": epoch-int, "path": "/local/.../<id>/<ts>.jpg", "note": str}. Immutable
    # tuple; newest-first. Absent in older records -> empty.
    photos: tuple[dict[str, Any], ...] = ()
    # v1.33 — latest biannual vision-health report (bounded by vision_health's rail
    # before it is stored here). A serialized HealthReport dict, or None if the plant
    # has never been assessed. Advisory only; absent in pre-v1.33 records -> None.
    health: dict[str, Any] | None = None
    # v1.35 — species (free text, common or scientific) + optimal-lux range for the
    # roaming light-survey feature. Range is both-set or both-None (validated by
    # light_survey.validate_lux_range at the service boundary; re-checked here so a
    # crafted .storage record can't smuggle NaN/inverted bounds into the verdict math).
    species: str = ""
    lux_low: int | None = None
    lux_high: int | None = None
    # v1.35 — newest-first bounded light-survey history (entries produced by
    # light_survey.make_survey_entry, re-cleaned on load by clean_stored_surveys).
    light_surveys: tuple[dict[str, Any], ...] = ()
    # v1.37 — pending species-ID suggestion (bounded by species_id.validate_suggestion
    # before storage; re-cleaned on load). None = no pending suggestion. Advisory:
    # nothing applies until the user accepts it.
    species_suggestion: dict[str, Any] | None = None
    # v1.38 — INSTALLED drip emitters on this plant (count x GPH), the physical
    # fact only the user knows. Feeds delivered-water math. Both-or-neither.
    emitter_count: int | None = None
    emitter_gph: float | None = None
    # v1.40.7 — the FULL attribute set the vision ID returns, persisted so nothing
    # the model found is dropped (advisory; each value was already bounded by
    # species_id.validate_suggestion before it reached the record). All optional;
    # absent in pre-v1.40.7 records -> the defaults below.
    common_name: str = ""  # e.g. "Oleander" (distinct from the free-text species)
    sunlight_class: str | None = None  # full_sun|partial_sun|bright_shade|deep_shade
    temp_low_f: int | None = None  # tolerated low/high (F); both-or-neither, low < high
    temp_high_f: int | None = None
    care_plan_preset: str | None = None  # tree|shrub|flower|cactus_succulent|grass
    water_every_days: int | None = None  # typical cadence (days) the model suggested
    fertilize_every_days: int | None = None  # 0 = not necessary
    id_confidence: float | None = None  # 0..1 — how sure the auto-ID was
    id_model: str = ""  # the vision model that produced the ID (e.g. "qwen2.5vl:7b")
    id_note: str = ""  # the model's one-line description of the plant
    identified_at: str = ""  # ISO timestamp of the identification

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("plant id must be non-empty")
        if not _SAFE_ID_RE.fullmatch(self.id):
            # Path-traversal / unsafe-segment guard (the id names a photo dir).
            raise ValueError(f"plant id must be 1-64 chars of [A-Za-z0-9_-], got {self.id!r}")
        if not self.name.strip():
            raise ValueError("plant name must be non-empty")
        if self.wucols_category not in WUCOLS_FACTORS:
            raise ValueError(
                f"unknown WUCOLS category {self.wucols_category!r}; "
                f"expected one of {sorted(WUCOLS_FACTORS)}"
            )
        # Reject NaN/Inf explicitly (NaN <= 0 is False, Inf > 0 is True, so a
        # bare "<= 0" check lets them through and they poison the water math).
        if not math.isfinite(self.canopy_area_sqft) or not (
            0 < self.canopy_area_sqft <= _MAX_CANOPY_AREA_SQFT
        ):
            raise ValueError(
                f"canopy_area_sqft must be a finite value in (0, {_MAX_CANOPY_AREA_SQFT}], "
                f"got {self.canopy_area_sqft}"
            )
        if not self.zone_entity_id:
            raise ValueError("zone_entity_id must be non-empty")
        # Map coordinates: either both set + finite + in [0, 1], or both None.
        for axis, val in (("map_x", self.map_x), ("map_y", self.map_y)):
            if val is not None and (not math.isfinite(val) or not (0.0 <= val <= 1.0)):
                raise ValueError(f"{axis} must be a finite value in [0, 1], got {val}")
        # v1.54 — area is a short free-text label ("" = ungrouped).
        if not isinstance(self.area, str) or len(self.area) > 60:
            raise ValueError("area must be a string of at most 60 chars")
        # Photos: a tuple of {ts, path, ...} dicts (each must at least have a path).
        if not isinstance(self.photos, tuple):
            raise ValueError("photos must be a tuple")
        for p in self.photos:
            if not isinstance(p, dict) or not p.get("path"):
                raise ValueError("each photo must be a dict with a non-empty 'path'")
        # Health: None or a dict (its contents are already bounded by the vision_health
        # rail before storage). Keep the record-level check light + advisory.
        if self.health is not None and not isinstance(self.health, dict):
            raise ValueError("health must be a dict or None")
        # v1.35 — species is a short free-text string; lux range is both-or-neither
        # with the same bounds the service boundary enforces.
        if not isinstance(self.species, str) or len(self.species) > 120:
            raise ValueError("species must be a string of at most 120 chars")
        if (self.lux_low is None) != (self.lux_high is None):
            raise ValueError("lux_low and lux_high must be set together")
        if self.lux_low is not None and self.lux_high is not None:
            from .light_survey import validate_lux_range

            validate_lux_range(self.lux_low, self.lux_high)
        if not isinstance(self.light_surveys, tuple):
            raise ValueError("light_surveys must be a tuple")
        if self.species_suggestion is not None and not isinstance(self.species_suggestion, dict):
            raise ValueError("species_suggestion must be a dict or None")
        # v1.40.7 — temp tolerance: both-or-neither with sane ordering (from_dict
        # sanitizes a stored bad pair to None, so this only guards live construction).
        if (self.temp_low_f is None) != (self.temp_high_f is None):
            raise ValueError("temp_low_f and temp_high_f must be set together")
        if (
            self.temp_low_f is not None
            and self.temp_high_f is not None
            and not (self.temp_low_f < self.temp_high_f)
        ):
            raise ValueError("temp_low_f must be less than temp_high_f")
        if (self.emitter_count is None) != (self.emitter_gph is None):
            raise ValueError("emitter_count and emitter_gph must be set together")
        if self.emitter_count is not None:
            if not isinstance(self.emitter_count, int) or not (1 <= self.emitter_count <= 100):
                raise ValueError("emitter_count must be an int in [1, 100]")
            if not math.isfinite(self.emitter_gph) or not (0.1 <= self.emitter_gph <= 50):
                raise ValueError("emitter_gph must be finite in [0.1, 50]")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "wucols_category": self.wucols_category,
            "canopy_area_sqft": self.canopy_area_sqft,
            "zone_entity_id": self.zone_entity_id,
            "map_x": self.map_x,
            "map_y": self.map_y,
            "anchor_lat": self.anchor_lat,
            "anchor_lon": self.anchor_lon,
            "area": self.area,
            "photos": [dict(p) for p in self.photos],
            "health": dict(self.health) if isinstance(self.health, dict) else None,
            "species": self.species,
            "lux_low": self.lux_low,
            "lux_high": self.lux_high,
            "light_surveys": [dict(s) for s in self.light_surveys],
            "species_suggestion": (
                dict(self.species_suggestion) if isinstance(self.species_suggestion, dict) else None
            ),
            "emitter_count": self.emitter_count,
            "emitter_gph": self.emitter_gph,
            # v1.40.7 — full auto-ID attribute set
            "common_name": self.common_name,
            "sunlight_class": self.sunlight_class,
            "temp_low_f": self.temp_low_f,
            "temp_high_f": self.temp_high_f,
            "care_plan_preset": self.care_plan_preset,
            "water_every_days": self.water_every_days,
            "fertilize_every_days": self.fertilize_every_days,
            "id_confidence": self.id_confidence,
            "id_model": self.id_model,
            "id_note": self.id_note,
            "identified_at": self.identified_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlantRecord:
        # Tolerant read: unknown keys are ignored so a future field (e.g.
        # installed emitters) doesn't break records written today. map_x/map_y
        # are absent in pre-v1.30 records -> None (unplaced); photos pre-v1.32 -> ().
        def _coord(key: str) -> float | None:
            v = data.get(key)
            return None if v is None else float(v)

        def _anchor(key: str, limit: float) -> float | None:
            # v1.59 — ground anchor. A junk/out-of-range value degrades to "no
            # anchor" rather than dropping the whole plant record on load.
            v = data.get(key)
            if v is None:
                return None
            try:
                f = float(v)
            except (TypeError, ValueError):
                return None
            return f if math.isfinite(f) and -limit <= f <= limit else None

        raw_photos = data.get("photos") or []
        # Photo paths are ALWAYS the server-set "/local/complete_irrigation/plants/..."
        # URL. Drop any entry whose path isn't a "/local/" relative URL on load, so a
        # hand-tampered .storage file can't inject e.g. a "javascript:" href into the
        # gallery's <a href> (defense-in-depth on top of the panel's escapeAttr).
        photos = tuple(
            dict(p)
            for p in raw_photos
            if isinstance(p, dict)
            and isinstance(p.get("path"), str)
            and p["path"].startswith("/local/")
        )
        # v1.35 — lux range: both-or-neither; a malformed pair degrades to None
        # (no range) rather than dropping the whole record on load.
        from .light_survey import clean_stored_surveys, validate_lux_range
        from .species_id import clean_stored_suggestion

        lux_low: int | None = None
        lux_high: int | None = None
        try:
            if data.get("lux_low") is not None and data.get("lux_high") is not None:
                lux_low, lux_high = validate_lux_range(data["lux_low"], data["lux_high"])
        except ValueError:
            lux_low = lux_high = None

        # v1.38 — installed emitters: both-or-neither; malformed degrades to None.
        _emitters: tuple[int | None, float | None] = (None, None)
        try:
            ec, eg = data.get("emitter_count"), data.get("emitter_gph")
            if ec is not None and eg is not None:
                ec_i, eg_f = int(ec), float(eg)
                if 1 <= ec_i <= 100 and math.isfinite(eg_f) and 0.1 <= eg_f <= 50:
                    _emitters = (ec_i, eg_f)
        except (TypeError, ValueError):
            _emitters = (None, None)

        # v1.40.7 — auto-ID attributes: forgiving read (advisory; a bad value
        # degrades to the default rather than dropping the whole record).
        def _opt_int(key: str, lo: int, hi: int) -> int | None:
            v = data.get(key)
            try:
                iv = int(v) if v is not None else None
            except (TypeError, ValueError):
                return None
            return iv if iv is not None and lo <= iv <= hi else None

        t_lo, t_hi = _opt_int("temp_low_f", -60, 160), _opt_int("temp_high_f", -60, 160)
        if t_lo is None or t_hi is None or not (t_lo < t_hi):
            t_lo = t_hi = None
        try:
            conf = data.get("id_confidence")
            conf = float(conf) if conf is not None else None
            if conf is not None and not (0.0 <= conf <= 1.0):
                conf = None
        except (TypeError, ValueError):
            conf = None
        _sun = data.get("sunlight_class")
        _preset = data.get("care_plan_preset")

        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            wucols_category=str(data["wucols_category"]),
            canopy_area_sqft=float(data["canopy_area_sqft"]),
            zone_entity_id=str(data["zone_entity_id"]),
            map_x=_coord("map_x"),
            map_y=_coord("map_y"),
            anchor_lat=_anchor("anchor_lat", 90.0),
            anchor_lon=_anchor("anchor_lon", 180.0),
            area=str(data.get("area") or "")[:60],
            photos=photos,
            health=data.get("health") if isinstance(data.get("health"), dict) else None,
            species=str(data.get("species") or "")[:120],
            lux_low=lux_low,
            lux_high=lux_high,
            light_surveys=clean_stored_surveys(data.get("light_surveys")),
            species_suggestion=clean_stored_suggestion(data.get("species_suggestion")),
            emitter_count=_emitters[0],
            emitter_gph=_emitters[1],
            common_name=str(data.get("common_name") or "")[:120],
            sunlight_class=_sun if isinstance(_sun, str) and _sun else None,
            temp_low_f=t_lo,
            temp_high_f=t_hi,
            care_plan_preset=_preset if isinstance(_preset, str) and _preset else None,
            water_every_days=_opt_int("water_every_days", 1, 60),
            fertilize_every_days=_opt_int("fertilize_every_days", 0, 3650),
            id_confidence=conf,
            id_model=str(data.get("id_model") or "")[:80],
            id_note=str(data.get("id_note") or "")[:300],
            identified_at=str(data.get("identified_at") or "")[:40],
        )

    def to_calc_plant(self) -> Plant:
        """Map to the pure hydraulics input type (loop_id = the zone)."""
        return Plant(
            name=self.name,
            wucols_category=self.wucols_category,
            canopy_area_sqft=self.canopy_area_sqft,
            loop_id=self.zone_entity_id,
            installed_count=self.emitter_count,
            installed_gph=self.emitter_gph,
        )


class PlantStore:
    """In-memory dict of PlantRecord by id, with serialization helpers.

    Insertion order is preserved (Python dict guarantee since 3.7).
    """

    def __init__(self) -> None:
        self._items: dict[str, PlantRecord] = {}

    # ── CRUD ────────────────────────────────────────────────────────
    def add(self, plant: PlantRecord) -> None:
        """Insert a new plant. Raises KeyError if id exists — use upsert()
        for overwrite semantics."""
        if plant.id in self._items:
            raise KeyError(f"plant with id {plant.id!r} already exists")
        self._items[plant.id] = plant

    def upsert(self, plant: PlantRecord) -> None:
        """Insert or replace by id."""
        self._items[plant.id] = plant

    def delete(self, plant_id: str) -> bool:
        """Remove the plant. Returns True if it existed, False if not."""
        return self._items.pop(plant_id, None) is not None

    def get(self, plant_id: str) -> PlantRecord | None:
        return self._items.get(plant_id)

    def all(self) -> list[PlantRecord]:
        """All plants in insertion order."""
        return list(self._items.values())

    def backfill_anchors(self, bbox: Any) -> int:
        """v1.59 — give every already-placed plant a durable GROUND anchor.

        Placements made before v1.59 exist only as map_x/map_y, i.e. relative to
        whatever frame was current when they were dropped — so the next re-fetch
        at a different centre/zoom silently erased them. Converting them once,
        against the frame they were placed in, makes existing yards durable
        immediately instead of only on the next map change.

        Returns the number of records upgraded. Idempotent: plants that already
        carry an anchor, and unplaced plants, are left alone.
        """
        from .yard_map import norm_to_latlon

        if bbox is None:
            return 0
        upgraded = 0
        for plant in list(self._items.values()):
            if plant.anchor_lat is not None and plant.anchor_lon is not None:
                continue
            if plant.map_x is None or plant.map_y is None:
                continue
            try:
                lat, lon = norm_to_latlon(float(plant.map_x), float(plant.map_y), bbox)
            except (TypeError, ValueError):
                continue
            self._items[plant.id] = replace(plant, anchor_lat=lat, anchor_lon=lon)
            upgraded += 1
        return upgraded

    def by_zone(self, zone_entity_id: str) -> list[PlantRecord]:
        """Plants attached to one zone/loop, in insertion order."""
        return [p for p in self._items.values() if p.zone_entity_id == zone_entity_id]

    def zone_ids(self) -> list[str]:
        """Distinct zone/loop ids that have at least one plant, in order."""
        seen: dict[str, None] = {}
        for p in self._items.values():
            seen.setdefault(p.zone_entity_id, None)
        return list(seen)

    # ── v1.54 light-area grouping ───────────────────────────────────
    def by_area(self, area: str) -> list[PlantRecord]:
        """Plants in one light-area (exact label match), in insertion order.
        An empty area matches the ungrouped plants."""
        want = (area or "").strip()
        return [p for p in self._items.values() if p.area == want]

    def areas(self) -> list[str]:
        """Distinct non-empty area labels that have at least one plant, in order."""
        seen: dict[str, None] = {}
        for p in self._items.values():
            if p.area:
                seen.setdefault(p.area, None)
        return list(seen)

    # ── Serialization ───────────────────────────────────────────────
    def to_serializable(self) -> list[dict[str, Any]]:
        """A JSON-safe list of plant dicts for HA storage."""
        return [p.to_dict() for p in self._items.values()]

    @classmethod
    def from_serializable(cls, data: list[dict[str, Any]]) -> PlantStore:
        """Build a fresh store from the serialized form.

        Per-record defensive: a single malformed/corrupt entry is logged and
        skipped rather than failing the whole load (which would take down
        list_plants / yard_report)."""
        store = cls()
        for d in data:
            try:
                rec = PlantRecord.from_dict(d)
            except (KeyError, ValueError, TypeError) as err:
                _LOGGER.warning("Skipping invalid plant record %r: %s", d, err)
                continue
            store._items[rec.id] = rec
        return store


def plants_in_region(
    plants: list[PlantRecord], x0: float, y0: float, x1: float, y1: float
) -> list[str]:
    """Ids of PLACED plants whose normalized (map_x, map_y) fall inside the box.

    Corners may be given in any order; unplaced plants (no map position) are
    skipped. Pure + HA-free: the panel draws a region on the aerial and this
    selects the enclosed markers to bulk-assign to a light area (v1.54)."""
    lo_x, hi_x = (x0, x1) if x0 <= x1 else (x1, x0)
    lo_y, hi_y = (y0, y1) if y0 <= y1 else (y1, y0)
    out: list[str] = []
    for p in plants:
        if p.map_x is None or p.map_y is None:
            continue
        if lo_x <= p.map_x <= hi_x and lo_y <= p.map_y <= hi_y:
            out.append(p.id)
    return out
