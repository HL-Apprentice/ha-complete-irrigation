"""Care-task reminders (v1.35) — recurring non-watering plant care.

The consumer plant-app reminder schema, adapted: a CareTask is "remind me about
<kind> for <plant or zone> every <interval> days", with last-done tracking so
completing a task re-arms the next reminder. Watering itself is NOT a care task —
schedules own watering; these are the fertilize / prune / mulch / inspect beats
that otherwise live on a kitchen calendar.

Pure + HA-free (same shape as PlantStore): the coordinator does the daily
due-check + notification; services own CRUD; this module owns records, due math,
and defensive (de)serialization.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, replace
from typing import Any

_LOGGER = logging.getLogger(__name__)

KIND_FERTILIZE = "fertilize"
KIND_PRUNE = "prune"
KIND_MULCH = "mulch"
KIND_INSPECT = "inspect"
KIND_CUSTOM = "custom"
CARE_KINDS = (KIND_FERTILIZE, KIND_PRUNE, KIND_MULCH, KIND_INSPECT, KIND_CUSTOM)

MIN_INTERVAL_DAYS = 1
MAX_INTERVAL_DAYS = 3650  # 10 years — beyond that it's not a reminder, it's an heirloom

_SAFE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_DAY = 86400


@dataclass(frozen=True)
class CareTask:
    """One recurring care reminder, attached to a plant OR a zone."""

    id: str
    kind: str
    interval_days: int
    # Exactly one of these should be set (a task is about a plant or a whole zone);
    # both-empty is rejected. plant_id wins if both are somehow present.
    plant_id: str = ""
    zone_entity_id: str = ""
    label: str = ""  # required for KIND_CUSTOM; optional nicety otherwise
    last_done_ts: int | None = None  # epoch; None = never done -> due immediately
    last_notified_ts: int | None = None  # epoch of the last due-reminder we sent
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.id or not _SAFE_ID_RE.fullmatch(self.id):
            raise ValueError(f"care-task id must be 1-64 chars of [A-Za-z0-9_-], got {self.id!r}")
        if self.kind not in CARE_KINDS:
            raise ValueError(f"unknown care kind {self.kind!r}; expected one of {CARE_KINDS}")
        if not isinstance(self.interval_days, int) or not (
            MIN_INTERVAL_DAYS <= self.interval_days <= MAX_INTERVAL_DAYS
        ):
            raise ValueError(
                f"interval_days must be an int in [{MIN_INTERVAL_DAYS}, {MAX_INTERVAL_DAYS}], "
                f"got {self.interval_days!r}"
            )
        if not self.plant_id and not self.zone_entity_id:
            raise ValueError("care task needs a plant_id or a zone_entity_id")
        if self.kind == KIND_CUSTOM and not self.label.strip():
            raise ValueError("custom care tasks need a label")
        for name, ts in (
            ("last_done_ts", self.last_done_ts),
            ("last_notified_ts", self.last_notified_ts),
        ):
            if ts is not None and (not isinstance(ts, int) or ts < 0):
                raise ValueError(f"{name} must be a non-negative int or None, got {ts!r}")

    # ── Due math ────────────────────────────────────────────────────
    def next_due_ts(self) -> int:
        """Epoch when this task is (or was) next due. Never-done -> 0 = due now."""
        if self.last_done_ts is None:
            return 0
        return self.last_done_ts + self.interval_days * _DAY

    def is_due(self, now_ts: int) -> bool:
        return self.enabled and now_ts >= self.next_due_ts()

    def needs_reminder(self, now_ts: int, *, renotify_days: int = 7) -> bool:
        """Due AND not already reminded within the renotify window — so a due
        task nags gently (weekly), not on every coordinator pass."""
        if not self.is_due(now_ts):
            return False
        if self.last_notified_ts is None:
            return True
        return now_ts - self.last_notified_ts >= renotify_days * _DAY

    def display_name(self) -> str:
        return self.label.strip() or self.kind.capitalize()

    def completed(self, now_ts: int) -> CareTask:
        """A new record stamped done-now (frozen dataclass -> replace)."""
        return replace(self, last_done_ts=int(now_ts), last_notified_ts=None)

    def notified(self, now_ts: int) -> CareTask:
        return replace(self, last_notified_ts=int(now_ts))

    # ── Serialization ───────────────────────────────────────────────
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "interval_days": self.interval_days,
            "plant_id": self.plant_id,
            "zone_entity_id": self.zone_entity_id,
            "label": self.label,
            "last_done_ts": self.last_done_ts,
            "last_notified_ts": self.last_notified_ts,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CareTask:
        def _opt_ts(key: str) -> int | None:
            v = data.get(key)
            if v is None:
                return None
            f = float(v)
            if not math.isfinite(f) or f < 0:
                raise ValueError(f"{key} must be a finite non-negative number")
            return int(f)

        return cls(
            id=str(data["id"]),
            kind=str(data["kind"]),
            interval_days=int(data["interval_days"]),
            plant_id=str(data.get("plant_id") or ""),
            zone_entity_id=str(data.get("zone_entity_id") or ""),
            label=str(data.get("label") or "")[:120],
            last_done_ts=_opt_ts("last_done_ts"),
            last_notified_ts=_opt_ts("last_notified_ts"),
            enabled=bool(data.get("enabled", True)),
        )


class CareTaskStore:
    """In-memory dict of CareTask by id (insertion-ordered), PlantStore-shaped."""

    def __init__(self) -> None:
        self._items: dict[str, CareTask] = {}

    def add(self, task: CareTask) -> None:
        if task.id in self._items:
            raise KeyError(f"care task with id {task.id!r} already exists")
        self._items[task.id] = task

    def upsert(self, task: CareTask) -> None:
        self._items[task.id] = task

    def delete(self, task_id: str) -> bool:
        return self._items.pop(task_id, None) is not None

    def get(self, task_id: str) -> CareTask | None:
        return self._items.get(task_id)

    def all(self) -> list[CareTask]:
        return list(self._items.values())

    def for_plant(self, plant_id: str) -> list[CareTask]:
        return [t for t in self._items.values() if t.plant_id == plant_id]

    def due(self, now_ts: int) -> list[CareTask]:
        return [t for t in self._items.values() if t.is_due(now_ts)]

    def needing_reminder(self, now_ts: int) -> list[CareTask]:
        return [t for t in self._items.values() if t.needs_reminder(now_ts)]

    def to_serializable(self) -> list[dict[str, Any]]:
        return [t.to_dict() for t in self._items.values()]

    @classmethod
    def from_serializable(cls, data: list[dict[str, Any]]) -> CareTaskStore:
        """Per-record defensive load: one corrupt entry is skipped, not fatal."""
        store = cls()
        for d in data:
            try:
                rec = CareTask.from_dict(d)
            except (KeyError, ValueError, TypeError) as err:
                _LOGGER.warning("Skipping invalid care task %r: %s", d, err)
                continue
            store._items[rec.id] = rec
        return store
