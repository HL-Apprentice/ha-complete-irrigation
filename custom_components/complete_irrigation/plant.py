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

from dataclasses import dataclass
from typing import Any

from .hydraulics import WUCOLS_FACTORS, Plant


@dataclass(frozen=True)
class PlantRecord:
    """One plant in the yard, attached to a zone/loop."""

    id: str
    name: str
    wucols_category: str
    canopy_area_sqft: float
    zone_entity_id: str

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("plant id must be non-empty")
        if not self.name.strip():
            raise ValueError("plant name must be non-empty")
        if self.wucols_category not in WUCOLS_FACTORS:
            raise ValueError(
                f"unknown WUCOLS category {self.wucols_category!r}; "
                f"expected one of {sorted(WUCOLS_FACTORS)}"
            )
        if self.canopy_area_sqft <= 0:
            raise ValueError(f"canopy_area_sqft must be positive, got {self.canopy_area_sqft}")
        if not self.zone_entity_id:
            raise ValueError("zone_entity_id must be non-empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "wucols_category": self.wucols_category,
            "canopy_area_sqft": self.canopy_area_sqft,
            "zone_entity_id": self.zone_entity_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlantRecord:
        # Tolerant read: unknown keys are ignored so a future field (e.g. map
        # location, installed emitters) doesn't break records written today.
        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            wucols_category=str(data["wucols_category"]),
            canopy_area_sqft=float(data["canopy_area_sqft"]),
            zone_entity_id=str(data["zone_entity_id"]),
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
        """Build a fresh store from the serialized form."""
        store = cls()
        for d in data:
            rec = PlantRecord.from_dict(d)
            store._items[rec.id] = rec
        return store
