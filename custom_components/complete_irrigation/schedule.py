"""Schedule data model + in-memory store (pure logic, no HA).

A Schedule is a user-defined recurring rule: run this zone at this
time, for this many minutes, on these weekdays.

The HA-coupled layer (coordinator + storage helper) wraps a
ScheduleStore instance and persists it to HA's storage on every change.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import date, time
from typing import Any

_LOGGER = logging.getLogger(__name__)

_VALID_WEEKDAYS = frozenset(range(7))  # 0=Mon .. 6=Sun (ISO)

SUN_EVENTS = ("sunrise", "sunset")
ANCHOR_START = "start"
ANCHOR_FINISH = "finish"
_VALID_ANCHORS = (ANCHOR_START, ANCHOR_FINISH)
MAX_SUN_OFFSET_MIN = 240

MODE_WEEKDAYS = "weekdays"
MODE_INTERVAL = "interval"
MODE_INTERVAL_HOURS = "interval_hours"
_VALID_MODES = (MODE_WEEKDAYS, MODE_INTERVAL, MODE_INTERVAL_HOURS)

# Default gap (seconds) between back-to-back zones in a multi-zone schedule.
# Lets the valve close fully before the next opens.
DEFAULT_ZONE_BUFFER_SECONDS = 30


def _parse_hhmm(s: str) -> time:
    """Parse 'HH:MM' into a `time`. Mirrors the inline parse used for
    start_time so interval_end_time uses the same format."""
    hh, mm = s.split(":")
    return time(int(hh), int(mm))


def _coerce_min_chunk(v: Any, duration_minutes: int) -> int | None:
    """v1.56 — tolerant read of min_chunk_minutes: a positive int clamped to the
    run's duration, else None (use the global default). Keeps a stale/oversized
    stored value from failing Schedule validation and dropping the whole record."""
    try:
        iv = int(v) if v is not None else None
    except (TypeError, ValueError):
        return None
    if iv is None or iv < 1:
        return None
    return min(iv, duration_minutes)


def _coerce_split_profile(v: Any) -> str:
    """v1.56 — tolerant read of split_profile: a known plant type, else "" (none)."""
    from .schedule_split import SPLIT_PROFILES

    s = str(v or "")
    return s if s in SPLIT_PROFILES else ""


@dataclass(frozen=True)
class ZoneStep:
    """One zone in a multi-zone schedule: which zone, how long.

    Multi-zone schedules carry an ordered tuple of these. The schedule's
    zone_entity_id + duration_minutes mirror the first step for
    backward compat with single-zone consumers.
    """

    zone_entity_id: str
    duration_minutes: int

    def __post_init__(self) -> None:
        if not self.zone_entity_id:
            raise ValueError("ZoneStep.zone_entity_id must be set")
        if self.duration_minutes <= 0:
            raise ValueError(
                f"ZoneStep.duration_minutes must be positive, got {self.duration_minutes}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "zone_entity_id": self.zone_entity_id,
            "duration_minutes": int(self.duration_minutes),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ZoneStep:
        return cls(
            zone_entity_id=data["zone_entity_id"],
            duration_minutes=int(data["duration_minutes"]),
        )


@dataclass(frozen=True)
class Schedule:
    """A single user-defined irrigation rule.

    Frozen — to "edit" a schedule, build a new one with the same id
    and pass it to ScheduleStore.upsert().

    Two recurrence modes:
      • weekdays — fires on the listed days of week, every week
      • interval — fires every interval_days days starting interval_anchor
    """

    id: str
    name: str
    zone_entity_id: str
    start_time: time
    duration_minutes: int
    # weekdays mode: which days of the week (0=Mon..6=Sun, ISO)
    weekdays: tuple[int, ...] = field(default_factory=tuple)
    enabled: bool = True
    # Active period bounds (v1.12). Together with end_date these form an
    # "active window" the schedule lives inside; outside the window it's
    # quiet but not deleted. `start_date` is the first date firings are
    # allowed (None = no lower bound, fires immediately). `end_date` is
    # the last allowed date (None = never end). `repeat_annually` makes
    # the window's month/day repeat each calendar year (seasonal mode);
    # when on, both dates are required and their month/day pair defines
    # the yearly window (year value is ignored on those fields).
    start_date: date | None = None
    repeat_annually: bool = False
    # Optional end_date — if set, schedule stops firing after this date.
    end_date: date | None = None
    # Recurrence mode: "weekdays" (default, current behavior) or "interval"
    mode: str = MODE_WEEKDAYS
    # interval mode: fire every N days starting interval_anchor
    interval_days: int | None = None
    interval_anchor: date | None = None
    # interval_hours mode: fire every N hours from interval_anchor +
    # start_time, continuing across day boundaries.
    interval_hours: int | None = None
    # Optional daily-window cap for interval_hours mode. When set:
    #   • the schedule fires every N hours from `start_time` ON EACH DAY
    #     (re-anchored daily, not continuous across midnight)
    #   • the last firing of a day is the latest one whose start ≤
    #     interval_end_time (inclusive)
    # When None, the legacy continuous-from-anchor behavior is preserved.
    interval_end_time: time | None = None
    # Multi-zone: ordered tuple of ZoneStep. When non-empty the schedule
    # fires zones back-to-back at run time (each waits for the previous to
    # finish, plus DEFAULT_ZONE_BUFFER_SECONDS). The first step mirrors
    # zone_entity_id + duration_minutes so single-zone consumers still work.
    zone_steps: tuple[ZoneStep, ...] = field(default_factory=tuple)
    # Provenance marker (PRD #60). Values: "panel" (created via UI),
    # "calendar" (created via HA calendar create_event), "service"
    # (created via add_schedule service call), or "establishment"
    # (auto-generated by start_establishment). Defaults to "panel" for
    # back-compat with schedules created before v1.10.
    created_via: str = "panel"
    # v1.17.3 — per-schedule weather-gate opt-outs. Useful for zones
    # like a bird bath fill, where wind defer (no spray drift to worry
    # about), hot-weather boost (you don't want EXTRA water; just fill
    # the bath), and rain lockout (the bath needs filling regardless
    # of weather) don't apply. Default false everywhere so existing
    # schedules behave identically.
    ignore_wind: bool = False
    ignore_hot_weather: bool = False
    ignore_rain_lockout: bool = False
    # v1.18 — optional color for visual identification. A hex string
    # like "#1e88e5" (validated against SCHEDULE_COLOR_PALETTE at the
    # service boundary, but the model accepts any "#rrggbb" string) or
    # None = no color (UI falls back to the default accent). Surfaces:
    # schedule-row left stripe, day-calendar pill tint.
    color: str | None = None
    # v1.40 — sun-anchored start (Irrigation Unlimited-inspired). When sun_event
    # is set, the occurrence's start is resolved per-day from that sun event ±
    # sun_offset_minutes; anchor="finish" back-computes the start so the WHOLE
    # schedule (all zone steps + buffers) COMPLETES at that moment ("finish at
    # sunrise"). start_time remains required and is the fallback whenever sun
    # times are unavailable (e.g. polar edge cases or provider failure).
    sun_event: str | None = None
    sun_offset_minutes: int = 0
    anchor: str = ANCHOR_START
    # v1.56 — scheduler priority. essential=True (default) means "keep this run as
    # pure as possible: on time and un-split; disrupt non-essential runs FIRST when
    # resolving a conflict." essential=False (e.g. a bird-bath fill) means the run
    # may be shifted AND split to fit around the essential runs, and yields first.
    # Nothing is ever dropped — the never-miss failsafe still applies to both.
    essential: bool = True
    # v1.56 — smallest slice this schedule may be split into when the scheduler
    # breaks a run to fit gaps (minutes). None = use the global default. Guards a
    # run from being chopped into useless slivers (e.g. a bird bath into 1-min bits).
    min_chunk_minutes: int | None = None
    # v1.56 — optional plant-type tag (tree/shrub/grass/flower/cactus_succulent).
    # When set, the split floor comes from that type's configurable default instead
    # of min_chunk_minutes, so re-tuning a type in Settings updates every schedule
    # of that type at once. "" = none (use min_chunk_minutes / the global default).
    split_profile: str = ""

    def __post_init__(self) -> None:
        # Validation split into small focused helpers — keeps each rule
        # adjacent to its error message and the whole flow scannable.
        self._validate_basics()
        if self.mode == MODE_WEEKDAYS:
            self._validate_weekdays_mode()
        elif self.mode == MODE_INTERVAL:
            self._validate_interval_mode()
        elif self.mode == MODE_INTERVAL_HOURS:
            self._validate_interval_hours_mode()
        self._validate_interval_end_time_only_in_hours_mode()
        self._validate_active_window()
        self._validate_zone_steps()
        self._validate_sun()

    def _validate_sun(self) -> None:
        if self.sun_event is not None and self.sun_event not in SUN_EVENTS:
            raise ValueError(
                f"sun_event must be one of {SUN_EVENTS} or None, got {self.sun_event!r}"
            )
        if not isinstance(self.sun_offset_minutes, int) or not (
            -MAX_SUN_OFFSET_MIN <= self.sun_offset_minutes <= MAX_SUN_OFFSET_MIN
        ):
            raise ValueError(
                f"sun_offset_minutes must be an int in [-{MAX_SUN_OFFSET_MIN}, "
                f"{MAX_SUN_OFFSET_MIN}], got {self.sun_offset_minutes!r}"
            )
        if self.anchor not in _VALID_ANCHORS:
            raise ValueError(f"anchor must be one of {_VALID_ANCHORS}, got {self.anchor!r}")
        if self.sun_event is not None and self.mode == MODE_INTERVAL_HOURS:
            raise ValueError("sun_event is not supported in interval_hours mode")

    def total_span_minutes(self, zone_buffer_seconds: int | None = None) -> float:
        """Total wall-clock span of one firing: all zone steps + inter-zone
        buffers (single-zone = duration_minutes). Used by the finish anchor."""
        buf = DEFAULT_ZONE_BUFFER_SECONDS if zone_buffer_seconds is None else zone_buffer_seconds
        if not self.zone_steps:
            return float(self.duration_minutes)
        total = sum(st.duration_minutes for st in self.zone_steps)
        return total + (len(self.zone_steps) - 1) * buf / 60.0

    def _validate_basics(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("name must be non-empty")
        if not self.zone_entity_id:
            raise ValueError("zone_entity_id must be set")
        if self.duration_minutes <= 0:
            raise ValueError(f"duration_minutes must be positive, got {self.duration_minutes}")
        if self.mode not in _VALID_MODES:
            raise ValueError(f"mode must be one of {_VALID_MODES}, got {self.mode!r}")
        # v1.56 — split-chunk floor: None (use default) or a positive minute count
        # no larger than the run itself.
        if self.min_chunk_minutes is not None and not (
            1 <= self.min_chunk_minutes <= self.duration_minutes
        ):
            raise ValueError(
                "min_chunk_minutes must be None or an int in "
                f"[1, {self.duration_minutes}], got {self.min_chunk_minutes}"
            )
        # v1.56 — split_profile is "" (none) or a known plant type.
        from .schedule_split import SPLIT_PROFILES

        if self.split_profile and self.split_profile not in SPLIT_PROFILES:
            raise ValueError(
                f"split_profile must be '' or one of {SPLIT_PROFILES}, got {self.split_profile!r}"
            )

    def _validate_weekdays_mode(self) -> None:
        if not self.weekdays:
            raise ValueError("weekdays mode requires non-empty weekdays")
        if not set(self.weekdays).issubset(_VALID_WEEKDAYS):
            raise ValueError(f"weekdays must be in 0..6, got {sorted(self.weekdays)}")

    def _validate_interval_mode(self) -> None:
        if self.interval_days is None or self.interval_days < 1:
            raise ValueError(f"interval mode requires interval_days >= 1, got {self.interval_days}")
        if self.interval_anchor is None:
            raise ValueError("interval mode requires interval_anchor (start date)")

    def _validate_interval_hours_mode(self) -> None:
        if self.interval_hours is None or self.interval_hours < 1:
            raise ValueError(
                f"interval_hours mode requires interval_hours >= 1, got {self.interval_hours}"
            )
        if self.interval_anchor is None:
            raise ValueError("interval_hours mode requires interval_anchor (start date)")
        # Daily-window cap (optional). When provided, must be strictly
        # after start_time so at least one firing fits in the window.
        if self.interval_end_time is not None and self.interval_end_time <= self.start_time:
            raise ValueError(
                "interval_end_time must be after start_time "
                f"(got {self.start_time} → {self.interval_end_time})"
            )

    def _validate_interval_end_time_only_in_hours_mode(self) -> None:
        if self.mode != MODE_INTERVAL_HOURS and self.interval_end_time is not None:
            raise ValueError("interval_end_time is only valid in interval_hours mode")

    def _validate_active_window(self) -> None:
        # Active period (v1.12): when repeat_annually is on, BOTH dates
        # are required and start.month-day must come before end.month-day
        # (we don't support windows that wrap across the year boundary in
        # this release — keeps the per-year membership check unambiguous).
        if not self.repeat_annually:
            return
        if self.start_date is None or self.end_date is None:
            raise ValueError("repeat_annually requires both start_date and end_date")
        start_md = (self.start_date.month, self.start_date.day)
        end_md = (self.end_date.month, self.end_date.day)
        if start_md > end_md:
            raise ValueError(
                "annually-repeating window must not wrap across year-end "
                "(start month-day must be on/before end month-day)"
            )

    def _validate_zone_steps(self) -> None:
        # Multi-zone: first step must agree with the top-level zone/duration
        # (the top-level fields stay authoritative for legacy single-zone
        # consumers; the steps tuple is an extension).
        if not self.zone_steps:
            return
        first = self.zone_steps[0]
        if first.zone_entity_id != self.zone_entity_id:
            raise ValueError("zone_steps[0].zone_entity_id must match top-level zone_entity_id")
        if first.duration_minutes != self.duration_minutes:
            raise ValueError("zone_steps[0].duration_minutes must match top-level duration_minutes")

    def with_changes(self, **overrides: Any) -> Schedule:
        """Return a new Schedule with the given field overrides.

        v1.15 — services.py previously rebuilt Schedule field-by-field
        whenever a single property changed (set_schedule_enabled,
        update_schedule), which silently dropped any field added to the
        dataclass after the handler was written. dataclasses.replace
        keeps the full field set in sync automatically and re-runs
        __post_init__ validation."""
        return replace(self, **overrides)

    def all_steps(self) -> tuple[ZoneStep, ...]:
        """Return the zone sequence. Falls back to a single ZoneStep
        built from the top-level fields when zone_steps is empty (legacy
        single-zone schedule)."""
        if self.zone_steps:
            return self.zone_steps
        return (
            ZoneStep(
                zone_entity_id=self.zone_entity_id,
                duration_minutes=self.duration_minutes,
            ),
        )

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
            "end_date": self.end_date.isoformat() if self.end_date else None,
            "mode": self.mode,
            "interval_days": self.interval_days,
            "interval_anchor": (self.interval_anchor.isoformat() if self.interval_anchor else None),
            "interval_hours": self.interval_hours,
            "interval_end_time": (
                self.interval_end_time.strftime("%H:%M") if self.interval_end_time else None
            ),
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "repeat_annually": self.repeat_annually,
            "zone_steps": [s.to_dict() for s in self.zone_steps],
            "created_via": self.created_via,
            "ignore_wind": self.ignore_wind,
            "ignore_hot_weather": self.ignore_hot_weather,
            "ignore_rain_lockout": self.ignore_rain_lockout,
            "color": self.color,
            "sun_event": self.sun_event,
            "sun_offset_minutes": self.sun_offset_minutes,
            "anchor": self.anchor,
            "essential": self.essential,
            "min_chunk_minutes": self.min_chunk_minutes,
            "split_profile": self.split_profile,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Schedule:
        """Parse from the serializable form. Backward compatible with
        pre-v1.4 schedules that have no `mode` field — they're treated
        as weekdays-mode. Pre-v1.8 schedules have no zone_steps."""
        hour_str, minute_str = data["start_time"].split(":")
        end_date_str = data.get("end_date")
        end_date_val = date.fromisoformat(end_date_str) if end_date_str else None
        anchor_str = data.get("interval_anchor")
        anchor_val = date.fromisoformat(anchor_str) if anchor_str else None
        mode = data.get("mode") or MODE_WEEKDAYS
        steps_raw = data.get("zone_steps") or []
        zone_steps = tuple(ZoneStep.from_dict(s) for s in steps_raw)

        return cls(
            id=data["id"],
            name=data["name"],
            zone_entity_id=data["zone_entity_id"],
            start_time=time(int(hour_str), int(minute_str)),
            duration_minutes=int(data["duration_minutes"]),
            weekdays=tuple(data.get("weekdays", ())),
            enabled=bool(data.get("enabled", True)),
            end_date=end_date_val,
            mode=mode,
            interval_days=data.get("interval_days"),
            interval_anchor=anchor_val,
            interval_hours=data.get("interval_hours"),
            interval_end_time=(
                _parse_hhmm(data["interval_end_time"]) if data.get("interval_end_time") else None
            ),
            start_date=(date.fromisoformat(data["start_date"]) if data.get("start_date") else None),
            repeat_annually=bool(data.get("repeat_annually", False)),
            zone_steps=zone_steps,
            created_via=data.get("created_via", "panel"),
            ignore_wind=bool(data.get("ignore_wind", False)),
            ignore_hot_weather=bool(data.get("ignore_hot_weather", False)),
            ignore_rain_lockout=bool(data.get("ignore_rain_lockout", False)),
            color=data.get("color") or None,
            # v1.40 — tolerant read: pre-v1.40 records have none of these.
            sun_event=(data.get("sun_event") if data.get("sun_event") in SUN_EVENTS else None),
            sun_offset_minutes=int(data.get("sun_offset_minutes") or 0),
            anchor=(data.get("anchor") if data.get("anchor") in _VALID_ANCHORS else ANCHOR_START),
            # v1.56 — tolerant read: pre-v1.56 records default to essential + no
            # custom split floor. A malformed min_chunk degrades to None.
            essential=bool(data.get("essential", True)),
            min_chunk_minutes=_coerce_min_chunk(
                data.get("min_chunk_minutes"), int(data["duration_minutes"])
            ),
            split_profile=_coerce_split_profile(data.get("split_profile")),
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
        """Build a fresh store from the serialized form.

        Per-record defensive (mirrors PlantStore): a single malformed / corrupt /
        hand-edited .storage entry is logged and skipped rather than raising out of
        async_setup — which would fail config-entry setup and take the WHOLE
        integration down (nothing waters) over one bad record."""
        store = cls()
        for d in data:
            try:
                sched = Schedule.from_dict(d)
            except (KeyError, ValueError, TypeError) as err:
                _LOGGER.warning("Skipping invalid schedule record %r: %s", d, err)
                continue
            store._items[sched.id] = sched
        return store


def is_within_active_window(sched: Schedule, check_date: date) -> bool:
    """Pure-logic check: is `check_date` within `sched`'s active window?

    Used by the run_planner to filter out firings before start_date or
    after end_date, with optional annual-repeat semantics.

    • repeat_annually = False (default): start_date / end_date are
      absolute one-shot bounds. check_date must satisfy
      start_date <= check_date <= end_date (whichever bounds are set).
    • repeat_annually = True: only the (month, day) of start/end are
      compared, year ignored. The window is required to be within a
      single calendar year (enforced at construction).
    """
    if not sched.repeat_annually:
        if sched.start_date is not None and check_date < sched.start_date:
            return False
        return not (sched.end_date is not None and check_date > sched.end_date)
    # Annual repeat — validation guarantees both dates are set + non-wrap.
    cur = (check_date.month, check_date.day)
    start = (sched.start_date.month, sched.start_date.day)
    end = (sched.end_date.month, sched.end_date.day)
    return start <= cur <= end
