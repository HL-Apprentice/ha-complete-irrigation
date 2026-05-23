"""Unit tests for run_history pure logic."""

from __future__ import annotations

# ruff: noqa: UP017
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.complete_irrigation.run_history import (
    HARD_RECORD_CAP,
    SOURCE_MANUAL,
    SOURCE_SCHEDULED,
    STATUS_ABORTED,
    STATUS_COMPLETED,
    STATUS_RUNNING,
    STATUS_SKIPPED,
    RunHistoryRecord,
    RunHistoryStore,
    _minutes_between,
)

UTC = timezone.utc


def _t(hour: int, minute: int = 0, day: int = 15) -> datetime:
    return datetime(2026, 5, day, hour, minute, tzinfo=UTC)


# ───────────────────────── RunHistoryRecord ──────────────────────────


def test_record_roundtrip_through_dict():
    rec = RunHistoryRecord(
        id="abc",
        started_at=_t(6).isoformat(),
        ended_at=_t(6, 15).isoformat(),
        zone_entity_id="switch.lawn",
        zone_name="Lawn",
        schedule_id="sch1",
        schedule_name="Morning",
        requested_minutes=15,
        actual_minutes=15,
        status=STATUS_COMPLETED,
        reason="",
        triggers={"moisture": {"combined_pct": 22.5}},
        source=SOURCE_SCHEDULED,
    )
    d = rec.to_dict()
    rec2 = RunHistoryRecord.from_dict(d)
    assert rec2 == rec


def test_record_from_dict_tolerates_missing_optional_fields():
    rec = RunHistoryRecord.from_dict(
        {
            "id": "x",
            "started_at": _t(6).isoformat(),
            "zone_entity_id": "switch.lawn",
            "requested_minutes": 10,
            "status": STATUS_COMPLETED,
            "source": SOURCE_MANUAL,
        }
    )
    assert rec.zone_name == "switch.lawn"  # defaults to entity_id
    assert rec.schedule_id is None
    assert rec.actual_minutes is None
    assert rec.triggers == {}


def test_with_changes_returns_new_instance_with_overrides():
    rec = RunHistoryRecord(
        id="x",
        started_at=_t(6).isoformat(),
        zone_entity_id="switch.lawn",
        zone_name="Lawn",
        requested_minutes=10,
        status=STATUS_RUNNING,
        source=SOURCE_MANUAL,
    )
    updated = rec.with_changes(status=STATUS_COMPLETED, actual_minutes=8)
    assert updated.status == STATUS_COMPLETED
    assert updated.actual_minutes == 8
    assert rec.status == STATUS_RUNNING  # original untouched


# ───────────────────── RunHistoryStore lifecycle ─────────────────────


def test_start_run_inserts_at_head_with_running_status():
    s = RunHistoryStore()
    rec = s.start_run(
        zone_entity_id="switch.lawn",
        zone_name="Lawn",
        requested_minutes=10,
        source=SOURCE_MANUAL,
        started_at=_t(6),
    )
    assert rec.status == STATUS_RUNNING
    assert rec.actual_minutes is None
    assert rec.ended_at is None
    assert s.all() == [rec]


def test_complete_run_sets_ended_at_and_actual_minutes():
    s = RunHistoryStore()
    s.start_run(
        zone_entity_id="switch.lawn",
        zone_name="Lawn",
        requested_minutes=10,
        source=SOURCE_MANUAL,
        started_at=_t(6),
    )
    updated = s.complete_run("switch.lawn", ended_at=_t(6, 10))
    assert updated is not None
    assert updated.status == STATUS_COMPLETED
    assert updated.actual_minutes == 10
    assert updated.ended_at is not None


def test_complete_run_uses_passed_actual_minutes_when_provided():
    s = RunHistoryStore()
    s.start_run(
        zone_entity_id="switch.lawn",
        zone_name="Lawn",
        requested_minutes=20,
        source=SOURCE_MANUAL,
        started_at=_t(6),
    )
    # actual_minutes overrides the computed delta
    updated = s.complete_run("switch.lawn", ended_at=_t(7), actual_minutes=42)
    assert updated.actual_minutes == 42


def test_abort_run_marks_aborted_and_records_partial_runtime():
    s = RunHistoryStore()
    s.start_run(
        zone_entity_id="switch.lawn",
        zone_name="Lawn",
        requested_minutes=20,
        source=SOURCE_MANUAL,
        started_at=_t(6),
    )
    updated = s.abort_run("switch.lawn", ended_at=_t(6, 7), reason="user pressed Stop")
    assert updated.status == STATUS_ABORTED
    assert updated.actual_minutes == 7
    assert updated.reason == "user pressed Stop"


def test_complete_returns_none_when_no_open_run():
    s = RunHistoryStore()
    assert s.complete_run("switch.lawn", ended_at=_t(6)) is None
    assert s.abort_run("switch.lawn", ended_at=_t(6)) is None


def test_starting_second_run_for_same_entity_closes_first():
    """Defensive: services layer should have stopped first, but if not,
    invariant is at most one open record per zone."""
    s = RunHistoryStore()
    s.start_run(
        zone_entity_id="switch.lawn",
        zone_name="Lawn",
        requested_minutes=10,
        source=SOURCE_MANUAL,
        started_at=_t(6),
    )
    s.start_run(
        zone_entity_id="switch.lawn",
        zone_name="Lawn",
        requested_minutes=15,
        source=SOURCE_SCHEDULED,
        started_at=_t(7),
    )
    open_records = [r for r in s.all() if r.status == STATUS_RUNNING]
    assert len(open_records) == 1
    assert open_records[0].source == SOURCE_SCHEDULED


def test_latest_open_filters_by_zone_and_status():
    s = RunHistoryStore()
    s.start_run(
        zone_entity_id="switch.lawn",
        zone_name="Lawn",
        requested_minutes=10,
        source=SOURCE_MANUAL,
        started_at=_t(6),
    )
    s.start_run(
        zone_entity_id="switch.garden",
        zone_name="Garden",
        requested_minutes=8,
        source=SOURCE_MANUAL,
        started_at=_t(6, 30),
    )
    s.complete_run("switch.lawn", ended_at=_t(6, 10))
    assert s.latest_open("switch.lawn") is None
    assert s.latest_open("switch.garden") is not None


# ───────────────────────── Skipped runs ──────────────────────────────


def test_record_skipped_inserts_completed_record_with_zero_runtime():
    s = RunHistoryStore()
    rec = s.record_skipped(
        zone_entity_id="switch.lawn",
        zone_name="Lawn",
        requested_minutes=15,
        schedule_id="sch1",
        schedule_name="Morning",
        fired_at=_t(6),
        reason="moisture above target",
        triggers={"moisture": {"combined_pct": 38.0, "target": 31.0}},
    )
    assert rec.status == STATUS_SKIPPED
    assert rec.actual_minutes == 0
    assert rec.started_at == rec.ended_at
    assert rec.reason == "moisture above target"
    assert rec.triggers["moisture"]["combined_pct"] == 38.0


# ────────────────────────── Retention ────────────────────────────────


def test_prune_drops_records_older_than_retention_days():
    s = RunHistoryStore()
    # Old run: 95 days ago
    s.start_run(
        zone_entity_id="switch.lawn",
        zone_name="Lawn",
        requested_minutes=10,
        source=SOURCE_MANUAL,
        started_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    # Recent run
    s.start_run(
        zone_entity_id="switch.lawn",
        zone_name="Lawn",
        requested_minutes=10,
        source=SOURCE_MANUAL,
        started_at=datetime(2026, 5, 14, tzinfo=UTC),
    )
    now = datetime(2026, 5, 15, tzinfo=UTC)
    removed = s.prune(now=now, retention_days=90)
    assert removed == 1
    assert len(s.all()) == 1


def test_prune_enforces_hard_cap():
    s = RunHistoryStore()
    base = datetime(2026, 5, 1, tzinfo=UTC)
    # Insert HARD_RECORD_CAP + 5 records, all within retention
    for i in range(HARD_RECORD_CAP + 5):
        s.start_run(
            zone_entity_id=f"switch.z{i}",
            zone_name=f"Z{i}",
            requested_minutes=1,
            source=SOURCE_MANUAL,
            started_at=base + timedelta(minutes=i),
        )
    # All within 90 days, but hard_cap should still trim to 1000
    removed = s.prune(now=datetime(2026, 5, 15, tzinfo=UTC), retention_days=90)
    assert removed == 5
    assert len(s.all()) == HARD_RECORD_CAP


def test_prune_keeps_records_when_all_recent():
    s = RunHistoryStore()
    s.start_run(
        zone_entity_id="switch.lawn",
        zone_name="Lawn",
        requested_minutes=10,
        source=SOURCE_MANUAL,
        started_at=datetime(2026, 5, 14, tzinfo=UTC),
    )
    removed = s.prune(now=datetime(2026, 5, 15, tzinfo=UTC), retention_days=90)
    assert removed == 0
    assert len(s.all()) == 1


def test_clear_drops_all_records():
    s = RunHistoryStore()
    s.start_run(
        zone_entity_id="switch.lawn",
        zone_name="Lawn",
        requested_minutes=10,
        source=SOURCE_MANUAL,
        started_at=_t(6),
    )
    s.start_run(
        zone_entity_id="switch.garden",
        zone_name="Garden",
        requested_minutes=8,
        source=SOURCE_MANUAL,
        started_at=_t(7),
    )
    removed = s.clear()
    assert removed == 2
    assert s.all() == []


# ─────────────────────── Serialization ───────────────────────────────


def test_store_roundtrip_through_serializable():
    s = RunHistoryStore()
    s.start_run(
        zone_entity_id="switch.lawn",
        zone_name="Lawn",
        requested_minutes=10,
        source=SOURCE_SCHEDULED,
        schedule_id="sch1",
        schedule_name="Morning",
        started_at=_t(6),
        triggers={"wind": {"mph": 8, "threshold": 15, "deferred": False}},
    )
    s.complete_run("switch.lawn", ended_at=_t(6, 10))
    s.record_skipped(
        zone_entity_id="switch.garden",
        zone_name="Garden",
        requested_minutes=5,
        schedule_id="sch2",
        schedule_name="Garden daily",
        fired_at=_t(7),
        reason="moisture above target",
    )

    raw = s.to_serializable()
    s2 = RunHistoryStore.from_serializable(raw)
    assert len(s2.all()) == 2
    # Newest-first order preserved
    assert s2.all()[0].zone_entity_id == "switch.garden"
    assert s2.all()[1].triggers == {"wind": {"mph": 8, "threshold": 15, "deferred": False}}


def test_from_serializable_handles_empty_list():
    assert RunHistoryStore.from_serializable([]).all() == []
    assert RunHistoryStore.from_serializable(None).all() == []  # type: ignore[arg-type]


# ────────────────── helper: _minutes_between ─────────────────────────


@pytest.mark.parametrize(
    "started,ended,expected",
    [
        (_t(6, 0), _t(6, 10), 10),
        (_t(6, 0), _t(6, 0), 0),
        (_t(6, 10), _t(6, 0), 0),  # negative clamps to 0
        (_t(6, 0), _t(6, 7), 7),
    ],
)
def test_minutes_between(started: datetime, ended: datetime, expected: int):
    assert _minutes_between(started.isoformat(), ended) == expected


def test_minutes_between_handles_naive_iso_input():
    # ISO without timezone — assumed UTC, no exception.
    assert _minutes_between("2026-05-15T06:00:00", _t(6, 30)) == 30
