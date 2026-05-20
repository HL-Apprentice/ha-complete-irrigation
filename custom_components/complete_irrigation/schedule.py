"""Schedule data model + in-memory store (pure logic, no HA).

A Schedule is a user-defined recurring rule: run this zone at this
time, for this many minutes, on these weekdays.

The HA-coupled layer (coordinator + storage helper) wraps a
ScheduleStore instance and persists it to HA's storage on every change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time
from typing import Any

_VALID_WEEKDAYS = frozenset(range(7))  # 0=Mon .. 6=Sun (ISO)


@dataclass(frozen=True)
class Schedule:
    """A single user-defined irrigation rule.

    Frozen — to "edit" a schedule, build a new one with the same id
    and pass it to ScheduleStore.upsert().
    """

    id: str
    name: str
    zone_entity_id: str
    start_time: time
    duration_minutes: int
    weekdays: tuple[int, ...] = field(default_factory=tuple)
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("name must be non-empty")
        if not self.zone_entity_id:
            raise ValueError("zone_entity_id must be set")
        if self.duration_minutes <= 0:
            raise ValueError(f"duration_minutes must be positive, got {self.duration_minutes}")
        if not self.weekdays:
            raise ValueError("weekdays must be non-empty")
        if not set(self.weekdays).issubset(_VALID_WEEKDAYS):
            raise ValueError(f"weekdays must be in 0..6, got {sorted(self.weekdays)}")

    def to_dict(self) -> dict[str, Any]:
        """Serializable form (lists + strings; HA storage stores JSON)."""
        return {
            "id": self.id,
            "name": self.name,
            "zone_entity_id": self.zone_entity_id,
            "start_time": self.start_time.strftime("%H:%M"),
            "duration_minutes": self.duration_minutes,
            "weekdays": list(self.weekdays),
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Schedule:
        """Parse from the serializable form. Missing `enabled` defaults to True."""
        hour_str, minute_str = data["start_time"].split(":")
        return cls(
            id=data["id"],
            name=data["name"],
            zone_entity_id=data["zone_entity_id"],
            start_time=time(int(hour_str), int(minute_str)),
            duration_minutes=int(data["duration_minutes"]),
            weekdays=tuple(data["weekdays"]),
            enabled=bool(data.get("enabled", True)),
        )


class ScheduleStore:
    """In-memory dict of Schedule by id, with serialization helpers.

    Insertion order is preserved (Python dict guarantee since 3.7).
    """

    def __init__(self) -> None:
        self._items: dict[str, Schedule] = {}

    # ── CRUD ────────────────────────────────────────────────────────
    def add(self, schedule: Schedule) -> None:
        """Insert a new schedule. Raises KeyError if id exists — use
        upsert() if you want overwrite semantics."""
        if schedule.id in self._items:
            raise KeyError(f"schedule with id {schedule.id!r} already exists")
        self._items[schedule.id] = schedule

    def upsert(self, schedule: Schedule) -> None:
        """Insert or replace by id."""
        self._items[schedule.id] = schedule

    def delete(self, schedule_id: str) -> bool:
        """Remove the schedule. Returns True if it existed, False if not."""
        return self._items.pop(schedule_id, None) is not None

    def get(self, schedule_id: str) -> Schedule | None:
        return self._items.get(schedule_id)

    def all(self) -> list[Schedule]:
        """All schedules in insertion order."""
        return list(self._items.values())

    def enabled_schedules(self) -> list[Schedule]:
        """Only those with enabled=True, in insertion order."""
        return [s for s in self._items.values() if s.enabled]

    # ── Serialization ───────────────────────────────────────────────
    def to_serializable(self) -> list[dict[str, Any]]:
        """A JSON-safe list of schedule dicts for HA storage."""
        return [s.to_dict() for s in self._items.values()]

    @classmethod
    def from_serializable(cls, data: list[dict[str, Any]]) -> ScheduleStore:
        """Build a fresh store from the serialized form."""
        store = cls()
        for d in data:
            store._items[d["id"]] = Schedule.from_dict(d)
        return store
