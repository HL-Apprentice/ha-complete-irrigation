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
from dataclasses import dataclass
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "wucols_category": self.wucols_category,
            "canopy_area_sqft": self.canopy_area_sqft,
            "zone_entity_id": self.zone_entity_id,
            "map_x": self.map_x,
            "map_y": self.map_y,
            "photos": [dict(p) for p in self.photos],
            "health": dict(self.health) if isinstance(self.health, dict) else None,
            "species": self.species,
            "lux_low": self.lux_low,
            "lux_high": self.lux_high,
            "light_surveys": [dict(s) for s in self.light_surveys],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlantRecord:
        # Tolerant read: unknown keys are ignored so a future field (e.g.
        # installed emitters) doesn't break records written today. map_x/map_y
        # are absent in pre-v1.30 records -> None (unplaced); photos pre-v1.32 -> ().
        def _coord(key: str) -> float | None:
            v = data.get(key)
            return None if v is None else float(v)

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

        lux_low: int | None = None
        lux_high: int | None = None
        try:
            if data.get("lux_low") is not None and data.get("lux_high") is not None:
                lux_low, lux_high = validate_lux_range(data["lux_low"], data["lux_high"])
        except ValueError:
            lux_low = lux_high = None

        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            wucols_category=str(data["wucols_category"]),
            canopy_area_sqft=float(data["canopy_area_sqft"]),
            zone_entity_id=str(data["zone_entity_id"]),
            map_x=_coord("map_x"),
            map_y=_coord("map_y"),
            photos=photos,
            health=data.get("health") if isinstance(data.get("health"), dict) else None,
            species=str(data.get("species") or "")[:120],
            lux_low=lux_low,
            lux_high=lux_high,
            light_surveys=clean_stored_surveys(data.get("light_surveys")),
        )

    def to_calc_plant(self) -> Plant:
        """Map to the pure hydraulics input type (loop_id = the zone)."""
        return Plant(
            name=self.name,
            wucols_category=self.wucols_category,
            canopy_area_sqft=self.canopy_area_sqft,
            loop_id=self.zone_entity_id,
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

    def by_zone(self, zone_entity_id: str) -> list[PlantRecord]:
        """Plants attached to one zone/loop, in insertion order."""
        return [p for p in self._items.values() if p.zone_entity_id == zone_entity_id]

    def zone_ids(self) -> list[str]:
        """Distinct zone/loop ids that have at least one plant, in order."""
        seen: dict[str, None] = {}
        for p in self._items.values():
            seen.setdefault(p.zone_entity_id, None)
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
