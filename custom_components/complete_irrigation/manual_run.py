"""Manual-run pure logic — no HA dependency.

Used by the `complete_irrigation.run_zone` service handler in `__init__.py`
to track and validate one-off zone runs triggered from the panel or
Developer Tools.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


def validate_run_duration(minutes: int | float, max_minutes: int = 60) -> int:
    """Validate + coerce a manual-run duration in minutes.

    Returns the duration as an int. Raises:
      - TypeError if not numeric
      - ValueError if <= 0 or > max_minutes

    `max_minutes` defaults to 60 — a safety cap that prevents typos
    (e.g. 600 instead of 60) from running a zone for ten hours.
    """
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)):
        raise TypeError(f"duration must be a number, got {type(minutes).__name__}")
    rounded = round(minutes)
    if rounded <= 0:
        raise ValueError(f"duration must be positive, got {minutes}")
    if rounded > max_minutes:
        raise ValueError(f"duration {rounded} exceeds max of {max_minutes} minutes")
    return rounded


@dataclass(frozen=True)
class ManualRun:
    """A single manual-run in progress.

    `start_at` and `duration` are immutable once the run starts. To
    extend a run, replace it in the tracker with a new instance.
    """

    entity_id: str
    start_at: datetime
    duration: timedelta

    def deadline(self) -> datetime:
        """When the run should auto-stop."""
        return self.start_at + self.duration

    def is_due(self, now: datetime) -> bool:
        """True if this run's deadline has been reached."""
        return now >= self.deadline()

    def time_remaining(self, now: datetime) -> timedelta:
        """How long until auto-stop (clamped to zero past the deadline)."""
        remaining = self.deadline() - now
        return remaining if remaining > timedelta(0) else timedelta(0)

    def time_remaining_seconds(self, now: datetime) -> int:
        """Integer seconds remaining — convenient for UI rendering."""
        return int(self.time_remaining(now).total_seconds())


class ManualRunTracker:
    """In-memory dict of currently-active manual runs, keyed by entity_id.

    One run per entity at a time — starting a new run for an entity that
    already has one replaces the previous (the auto-stop callback for
    the old run is the caller's responsibility to cancel).
    """

    def __init__(self) -> None:
        self._runs: dict[str, ManualRun] = {}

    def start(self, run: ManualRun) -> None:
        """Record a started run, replacing any previous run for the same entity."""
        self._runs[run.entity_id] = run

    def stop(self, entity_id: str) -> ManualRun | None:
        """Remove and return the run for `entity_id`, or None if no run is active."""
        return self._runs.pop(entity_id, None)

    def get(self, entity_id: str) -> ManualRun | None:
        """Return the active run for `entity_id`, or None."""
        return self._runs.get(entity_id)

    def active(self) -> list[ManualRun]:
        """All currently-tracked runs."""
        return list(self._runs.values())

    def due_runs(self, now: datetime) -> list[ManualRun]:
        """Runs whose deadline has been reached as of `now`."""
        return [r for r in self._runs.values() if r.is_due(now)]
