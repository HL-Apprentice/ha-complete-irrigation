"""Tests for in_quiet_hours pure logic."""

from __future__ import annotations

from datetime import datetime

from custom_components.complete_irrigation.notifications import in_quiet_hours


def test_quiet_hours_wrap_around_midnight():
    """Default 22:00-07:00 covers late night and early morning."""
    assert in_quiet_hours(datetime(2026, 5, 18, 23, 0), "22:00", "07:00") is True
    assert in_quiet_hours(datetime(2026, 5, 19, 3, 0), "22:00", "07:00") is True
    assert in_quiet_hours(datetime(2026, 5, 19, 6, 59), "22:00", "07:00") is True
    assert in_quiet_hours(datetime(2026, 5, 19, 7, 0), "22:00", "07:00") is False
    assert in_quiet_hours(datetime(2026, 5, 19, 12, 0), "22:00", "07:00") is False
    assert in_quiet_hours(datetime(2026, 5, 19, 21, 59), "22:00", "07:00") is False


def test_quiet_hours_no_wrap():
    """Same-day quiet window (e.g. 09:00-17:00)."""
    assert in_quiet_hours(datetime(2026, 5, 18, 10, 0), "09:00", "17:00") is True
    assert in_quiet_hours(datetime(2026, 5, 18, 17, 0), "09:00", "17:00") is False
    assert in_quiet_hours(datetime(2026, 5, 18, 8, 59), "09:00", "17:00") is False


def test_quiet_hours_edge_at_start():
    """Exactly at start → inside the window."""
    assert in_quiet_hours(datetime(2026, 5, 18, 22, 0), "22:00", "07:00") is True
