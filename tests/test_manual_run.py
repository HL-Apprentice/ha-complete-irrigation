"""Tests for manual-run pure logic (no HA dependency).

The HA-coupled service handler in `__init__.py` (next commit) will use
these to record / look up / expire active manual runs.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from custom_components.complete_irrigation.manual_run import (
    ManualRun,
    ManualRunTracker,
    validate_run_duration,
)

NOW = datetime(2026, 5, 19, 14, 30, 0)


# ════════════════════════════════════════════════════════════════════
# validate_run_duration
# ════════════════════════════════════════════════════════════════════


def test_accepts_int_in_range():
    assert validate_run_duration(10) == 10
    assert validate_run_duration(1) == 1
    assert validate_run_duration(60) == 60


def test_coerces_float_to_rounded_int():
    assert validate_run_duration(5.0) == 5
    assert validate_run_duration(5.4) == 5
    assert validate_run_duration(5.6) == 6


def test_rejects_zero_minutes():
    with pytest.raises(ValueError, match="must be positive"):
        validate_run_duration(0)


def test_rejects_negative_minutes():
    with pytest.raises(ValueError, match="must be positive"):
        validate_run_duration(-1)


def test_rejects_over_default_max_60():
    with pytest.raises(ValueError, match="exceeds max"):
        validate_run_duration(61)


def test_custom_max_minutes():
    assert validate_run_duration(90, max_minutes=120) == 90
    assert validate_run_duration(120, max_minutes=120) == 120
    with pytest.raises(ValueError, match="exceeds max"):
        validate_run_duration(121, max_minutes=120)


def test_rejects_non_numeric():
    with pytest.raises(TypeError):
        validate_run_duration("ten")
    with pytest.raises(TypeError):
        validate_run_duration(None)
    with pytest.raises(TypeError):
        validate_run_duration([5])


# ════════════════════════════════════════════════════════════════════
# ManualRun
# ════════════════════════════════════════════════════════════════════


def test_deadline_is_start_plus_duration():
    run = ManualRun(
        entity_id="switch.front_lawn",
        start_at=NOW,
        duration=timedelta(minutes=10),
    )
    assert run.deadline() == NOW + timedelta(minutes=10)


def test_not_due_before_deadline():
    run = ManualRun("switch.x", NOW, timedelta(minutes=5))
    assert run.is_due(NOW) is False
    assert run.is_due(NOW + timedelta(minutes=4, seconds=59)) is False


def test_due_at_and_after_deadline():
    run = ManualRun("switch.x", NOW, timedelta(minutes=5))
    assert run.is_due(NOW + timedelta(minutes=5)) is True
    assert run.is_due(NOW + timedelta(minutes=10)) is True


def test_time_remaining_counts_down():
    run = ManualRun("switch.x", NOW, timedelta(minutes=10))
    assert run.time_remaining(NOW) == timedelta(minutes=10)
    assert run.time_remaining(NOW + timedelta(minutes=3)) == timedelta(minutes=7)
    assert run.time_remaining(NOW + timedelta(minutes=10)) == timedelta(0)


def test_time_remaining_clamped_to_zero_past_deadline():
    run = ManualRun("switch.x", NOW, timedelta(minutes=5))
    assert run.time_remaining(NOW + timedelta(minutes=15)) == timedelta(0)


def test_time_remaining_seconds_helper_for_ui():
    """Panel renders countdown as integer seconds — needs this helper."""
    run = ManualRun("switch.x", NOW, timedelta(minutes=7, seconds=21))
    assert run.time_remaining_seconds(NOW) == 7 * 60 + 21
    assert run.time_remaining_seconds(NOW + timedelta(minutes=3)) == 4 * 60 + 21
    assert run.time_remaining_seconds(NOW + timedelta(minutes=10)) == 0


# ════════════════════════════════════════════════════════════════════
# ManualRunTracker
# ════════════════════════════════════════════════════════════════════


def test_tracker_starts_empty():
    tracker = ManualRunTracker()
    assert tracker.active() == []


def test_tracker_records_started_run():
    tracker = ManualRunTracker()
    run = ManualRun("switch.front", NOW, timedelta(minutes=10))
    tracker.start(run)
    assert tracker.get("switch.front") == run
    assert tracker.active() == [run]


def test_tracker_stop_removes_and_returns():
    tracker = ManualRunTracker()
    run = ManualRun("switch.front", NOW, timedelta(minutes=10))
    tracker.start(run)
    popped = tracker.stop("switch.front")
    assert popped == run
    assert tracker.get("switch.front") is None
    assert tracker.active() == []


def test_tracker_stop_unknown_returns_none():
    tracker = ManualRunTracker()
    assert tracker.stop("switch.nope") is None


def test_tracker_restart_same_entity_replaces_previous():
    """Starting a run for an entity that already has one replaces it
    (use case: user changes their mind and reruns with new duration)."""
    tracker = ManualRunTracker()
    first = ManualRun("switch.x", NOW, timedelta(minutes=5))
    second = ManualRun("switch.x", NOW + timedelta(minutes=1), timedelta(minutes=15))
    tracker.start(first)
    tracker.start(second)
    assert tracker.get("switch.x") == second
    assert tracker.active() == [second]


def test_tracker_multiple_entities_held_independently():
    tracker = ManualRunTracker()
    r1 = ManualRun("switch.a", NOW, timedelta(minutes=5))
    r2 = ManualRun("switch.b", NOW, timedelta(minutes=10))
    tracker.start(r1)
    tracker.start(r2)
    assert set(tracker.active()) == {r1, r2}
    tracker.stop("switch.a")
    assert tracker.active() == [r2]


def test_tracker_due_runs_filters_by_deadline():
    tracker = ManualRunTracker()
    expired = ManualRun("switch.a", NOW, timedelta(minutes=5))
    fresh = ManualRun("switch.b", NOW + timedelta(minutes=4), timedelta(minutes=10))
    tracker.start(expired)
    tracker.start(fresh)

    # At NOW + 6 min: expired's deadline (NOW + 5 min) has passed,
    # fresh's deadline (NOW + 4 min + 10 min = NOW + 14 min) has not.
    due = tracker.due_runs(NOW + timedelta(minutes=6))
    assert due == [expired]


def test_tracker_due_runs_empty_when_nothing_active():
    tracker = ManualRunTracker()
    assert tracker.due_runs(NOW) == []
