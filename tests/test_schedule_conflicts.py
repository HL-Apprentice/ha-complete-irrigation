"""Schedule conflict detection (v1.56) — pure, HA-free."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from custom_components.complete_irrigation.schedule_conflicts import (
    conflicts_for_schedule,
    detect_conflicts,
    summarize_conflict,
)


@dataclass
class _Run:
    zone_entity_id: str
    start_at: datetime
    duration_minutes: int
    schedule_id: str
    schedule_name: str
    essential: bool = True


def _r(sid, zone, hh, mm, dur, essential=True):
    return _Run(zone, datetime(2026, 7, 20, hh, mm), dur, sid, sid, essential)


def test_overlapping_runs_conflict():
    runs = [
        _r("grass", "switch.a", 6, 0, 30, essential=True),
        _r("bath", "switch.b", 6, 15, 20, essential=False),  # starts inside grass
    ]
    cs = detect_conflicts(runs)
    assert len(cs) == 1
    c = cs[0]
    assert {c["a"].schedule_id, c["b"].schedule_id} == {"grass", "bath"}
    assert c["overlap_minutes"] == 15  # 6:15 -> 6:30
    assert c["essential_involved"] is True
    assert c["both_essential"] is False


def test_non_overlapping_no_conflict():
    runs = [
        _r("a", "switch.a", 6, 0, 15),
        _r("b", "switch.b", 6, 30, 15),  # starts after a ends (6:15)
    ]
    assert detect_conflicts(runs) == []


def test_buffer_extends_window_into_a_conflict():
    runs = [
        _r("a", "switch.a", 6, 0, 15),  # ends 6:15
        _r("b", "switch.b", 6, 16, 15),  # 1 min gap
    ]
    assert detect_conflicts(runs) == []  # no buffer -> fine
    assert len(detect_conflicts(runs, buffer_minutes=2)) == 1  # 2-min buffer -> collide


def test_independent_zone_never_conflicts():
    runs = [
        _r("a", "switch.a", 6, 0, 30),
        _r("pond", "switch.pond", 6, 10, 30),  # own supply
    ]
    assert detect_conflicts(runs, independent_zones=("switch.pond",)) == []


def test_both_essential_flagged():
    runs = [
        _r("grass1", "switch.a", 6, 0, 30, essential=True),
        _r("grass2", "switch.b", 6, 10, 30, essential=True),
    ]
    c = detect_conflicts(runs)[0]
    assert c["both_essential"] is True


def test_conflicts_for_schedule_filter():
    runs = [
        _r("a", "switch.a", 6, 0, 30),
        _r("b", "switch.b", 6, 10, 30),
        _r("c", "switch.c", 9, 0, 10),  # unrelated, no conflict
    ]
    cs = detect_conflicts(runs)
    assert [c for c in conflicts_for_schedule(cs, "b")]  # b is involved
    assert conflicts_for_schedule(cs, "c") == []  # c conflicts with nothing


def test_same_schedule_steps_never_conflict():
    # Two zone-steps of ONE multi-zone schedule are sequential by construction —
    # they must never be reported as conflicting (v1.56 fix).
    runs = [
        _Run("switch.a", datetime(2026, 7, 20, 6, 0), 10, "multi", "Multi"),
        _Run(
            "switch.b", datetime(2026, 7, 20, 6, 11), 10, "multi", "Multi"
        ),  # step 2, overlaps under buffer
    ]
    assert detect_conflicts(runs, buffer_minutes=2) == []


def test_summarize_conflict_wording():
    c = detect_conflicts(
        [
            _r("Grass", "switch.a", 6, 0, 30, essential=True),
            _r("Bird Bath", "switch.b", 6, 15, 20, essential=False),
        ]
    )[0]
    text = summarize_conflict(c)
    assert "Grass" in text and "Bird Bath" in text
    assert "essential" in text and "non-essential" in text
