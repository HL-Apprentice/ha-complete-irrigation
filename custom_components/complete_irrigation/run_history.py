"""Run history — pure logic, no HA dependency.

Records every irrigation run the integration knows about: scheduled
completions, scheduled skips (moisture/wind/lockout), manual one-off
runs, and aborted runs (user-pressed Stop or external switch-off).

Backed by a JSON store in coordinator.py. 90-day rolling retention
with a hard cap of 1000 records so the WS payload stays manageable
on large setups.

Records are append-only from the recorder's perspective; aborts and
completions mutate the *latest open record for an entity* by replacing
it with a new immutable dataclass — same pattern ScheduleStore uses.
"""

from __future__ import annotations

# UP017 (datetime.UTC) requires Python 3.11+; integration ships on 3.12
# but local dev / CI still has 3.9-compatible test runners floating around,
# so we stay on `timezone.utc`.
# ruff: noqa: UP017
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

UTC = timezone.utc

# Hard caps so the JSON store + WS payload don't grow unbounded.
DEFAULT_RETENTION_DAYS = 90
HARD_RECORD_CAP = 1000

# Status constants — string literals so the JSON round-trip is trivial.
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_ABORTED = "aborted"
STATUS_SKIPPED = "skipped"

VALID_STATUSES = (STATUS_RUNNING, STATUS_COMPLETED, STATUS_ABORTED, STATUS_SKIPPED)

# Source constants.
SOURCE_SCHEDULED = "scheduled"
SOURCE_MANUAL = "manual"


@dataclass(frozen=True)
class RunHistoryRecord:
    """One entry in the run history log.

    All datetime fields are ISO-format strings (timezone-aware) so the
    record serializes cleanly to JSON without custom encoders.
    """

    id: str
    started_at: str
    zone_entity_id: str
    zone_name: str
    requested_minutes: int
    status: str
    source: str
    schedule_id: str | None = None
    schedule_name: str = ""
    ended_at: str | None = None
    actual_minutes: int | None = None
    reason: str = ""
    triggers: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly representation."""
        return {
            "id": self.id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "zone_entity_id": self.zone_entity_id,
            "zone_name": self.zone_name,
            "schedule_id": self.schedule_id,
            "schedule_name": self.schedule_name,
            "requested_minutes": self.requested_minutes,
            "actual_minutes": self.actual_minutes,
            "status": self.status,
            "reason": self.reason,
            "triggers": dict(self.triggers),
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RunHistoryRecord:
        """Reverse of to_dict — tolerant of missing optional fields."""
        return cls(
            id=str(raw["id"]),
            started_at=str(raw["started_at"]),
            ended_at=raw.get("ended_at"),
            zone_entity_id=str(raw["zone_entity_id"]),
            zone_name=str(raw.get("zone_name", raw["zone_entity_id"])),
            schedule_id=raw.get("schedule_id"),
            schedule_name=str(raw.get("schedule_name", "")),
            requested_minutes=int(raw.get("requested_minutes", 0)),
            actual_minutes=(
                int(raw["actual_minutes"]) if raw.get("actual_minutes") is not None else None
            ),
            status=str(raw.get("status", STATUS_COMPLETED)),
            reason=str(raw.get("reason", "")),
            triggers=dict(raw.get("triggers") or {}),
            source=str(raw.get("source", SOURCE_MANUAL)),
        )

    def with_changes(self, **kwargs: Any) -> RunHistoryRecord:
        """Frozen-dataclass-friendly mutation helper. Returns a new record."""
        merged = self.to_dict()
        merged.update(kwargs)
        return RunHistoryRecord.from_dict(merged)


def new_record_id() -> str:
    """Stable id helper — exposed for tests that want deterministic ids
    via monkeypatch."""
    return uuid.uuid4().hex[:12]


class RunHistoryStore:
    """In-memory ordered list of RunHistoryRecord, newest-first.

    Append-only from the outside; `start_*` returns a new record id,
    and `complete` / `abort` mutate by id. `prune` enforces retention.
    """

    def __init__(self, records: list[RunHistoryRecord] | None = None) -> None:
        # Newest first so the WS payload + UI table can slice the head.
        self._records: list[RunHistoryRecord] = list(records or [])

    # ── Read ──────────────────────────────────────────────────────

    def all(self) -> list[RunHistoryRecord]:
        return list(self._records)

    def get(self, record_id: str) -> RunHistoryRecord | None:
        for r in self._records:
            if r.id == record_id:
                return r
        return None

    def latest_open(self, zone_entity_id: str) -> RunHistoryRecord | None:
        """The most recent record for `zone_entity_id` that is still in
        STATUS_RUNNING. Used to attach a completion/abort event to the
        right record without needing the id round-trip."""
        for r in self._records:
            if r.zone_entity_id == zone_entity_id and r.status == STATUS_RUNNING:
                return r
        return None

    def running_records(self) -> list[RunHistoryRecord]:
        """v1.26 — every record still in STATUS_RUNNING (at most one per zone).
        Surfaced in the health.json feed as the controller's currently-running
        zones, so the LLM monitor knows what's live this cycle."""
        return [r for r in self._records if r.status == STATUS_RUNNING]

    # ── Write — start a run ──────────────────────────────────────

    def start_run(
        self,
        *,
        zone_entity_id: str,
        zone_name: str,
        requested_minutes: int,
        source: str,
        started_at: datetime,
        schedule_id: str | None = None,
        schedule_name: str = "",
        triggers: dict[str, Any] | None = None,
        record_id: str | None = None,
    ) -> RunHistoryRecord:
        """Insert a new STATUS_RUNNING record at the head of the list.

        If any prior open record exists for this entity, force-close it
        as completed (defensive — should not normally happen since the
        services layer auto-stops first).
        """
        # Close any stale open record for this zone — should be rare but
        # keeps the invariant that latest_open() returns at most one.
        existing = self.latest_open(zone_entity_id)
        if existing is not None:
            self._replace(
                existing,
                existing.with_changes(
                    ended_at=started_at.astimezone(UTC).isoformat(),
                    status=STATUS_COMPLETED,
                    actual_minutes=existing.requested_minutes,
                ),
            )
        rec = RunHistoryRecord(
            id=record_id or new_record_id(),
            started_at=started_at.astimezone(UTC).isoformat(),
            zone_entity_id=zone_entity_id,
            zone_name=zone_name,
            requested_minutes=int(requested_minutes),
            status=STATUS_RUNNING,
            source=source,
            schedule_id=schedule_id,
            schedule_name=schedule_name,
            triggers=dict(triggers or {}),
        )
        self._records.insert(0, rec)
        return rec

    # ── Write — complete / abort a run ───────────────────────────

    def complete_run(
        self,
        zone_entity_id: str,
        *,
        ended_at: datetime,
        actual_minutes: int | None = None,
    ) -> RunHistoryRecord | None:
        """Mark the latest open run for `zone_entity_id` as completed.

        If `actual_minutes` is None, computes it from started_at→ended_at
        (rounded). Returns the updated record, or None if no open run.
        """
        existing = self.latest_open(zone_entity_id)
        if existing is None:
            return None
        actual = (
            actual_minutes
            if actual_minutes is not None
            else _minutes_between(existing.started_at, ended_at)
        )
        updated = existing.with_changes(
            ended_at=ended_at.astimezone(UTC).isoformat(),
            status=STATUS_COMPLETED,
            actual_minutes=int(actual),
        )
        self._replace(existing, updated)
        return updated

    def abort_run(
        self,
        zone_entity_id: str,
        *,
        ended_at: datetime,
        reason: str = "",
    ) -> RunHistoryRecord | None:
        """Mark the latest open run as aborted (user stop / external off).

        `actual_minutes` is computed from started_at→ended_at.
        """
        existing = self.latest_open(zone_entity_id)
        if existing is None:
            return None
        actual = _minutes_between(existing.started_at, ended_at)
        updated = existing.with_changes(
            ended_at=ended_at.astimezone(UTC).isoformat(),
            status=STATUS_ABORTED,
            actual_minutes=int(actual),
            reason=reason or existing.reason,
        )
        self._replace(existing, updated)
        return updated

    # ── Write — skipped runs ─────────────────────────────────────

    def record_skipped(
        self,
        *,
        zone_entity_id: str,
        zone_name: str,
        requested_minutes: int,
        schedule_id: str,
        schedule_name: str,
        fired_at: datetime,
        reason: str,
        triggers: dict[str, Any] | None = None,
    ) -> RunHistoryRecord:
        """Record a scheduled run that was skipped (moisture/wind/lockout).

        started_at == ended_at == fired_at; actual_minutes = 0.
        """
        ts = fired_at.astimezone(UTC).isoformat()
        rec = RunHistoryRecord(
            id=new_record_id(),
            started_at=ts,
            ended_at=ts,
            zone_entity_id=zone_entity_id,
            zone_name=zone_name,
            schedule_id=schedule_id,
            schedule_name=schedule_name,
            requested_minutes=int(requested_minutes),
            actual_minutes=0,
            status=STATUS_SKIPPED,
            reason=reason,
            triggers=dict(triggers or {}),
            source=SOURCE_SCHEDULED,
        )
        self._records.insert(0, rec)
        return rec

    # ── Retention ────────────────────────────────────────────────

    def prune(
        self,
        *,
        now: datetime,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        hard_cap: int = HARD_RECORD_CAP,
    ) -> int:
        """Drop records older than `retention_days` AND enforce `hard_cap`.

        Returns the number of records removed. Safe to call repeatedly.
        """
        cutoff = (now - timedelta(days=retention_days)).astimezone(UTC)
        kept: list[RunHistoryRecord] = []
        for r in self._records:
            try:
                started = datetime.fromisoformat(r.started_at)
            except ValueError:
                continue  # drop malformed
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            if started >= cutoff:
                kept.append(r)
        removed = len(self._records) - len(kept)
        if len(kept) > hard_cap:
            removed += len(kept) - hard_cap
            kept = kept[:hard_cap]
        self._records = kept
        return removed

    def clear(self) -> int:
        """Remove all records. Returns count removed."""
        n = len(self._records)
        self._records = []
        return n

    # ── Serialization ────────────────────────────────────────────

    def to_serializable(self) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self._records]

    @classmethod
    def from_serializable(cls, raw: list[dict[str, Any]]) -> RunHistoryStore:
        return cls([RunHistoryRecord.from_dict(d) for d in raw or []])

    # ── Internals ────────────────────────────────────────────────

    def _replace(self, old: RunHistoryRecord, new: RunHistoryRecord) -> None:
        """Replace `old` in place. Both must have the same id."""
        for i, r in enumerate(self._records):
            if r.id == old.id:
                self._records[i] = new
                return


def _minutes_between(started_iso: str, ended: datetime) -> int:
    """Whole minutes between an ISO timestamp string and a datetime.

    Rounds half-up; floor at 0 so a clock skew doesn't produce negatives.
    """
    try:
        started = datetime.fromisoformat(started_iso)
    except ValueError:
        return 0
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    delta = ended.astimezone(UTC) - started
    secs = int(delta.total_seconds())
    if secs <= 0:
        return 0
    return (secs + 30) // 60
