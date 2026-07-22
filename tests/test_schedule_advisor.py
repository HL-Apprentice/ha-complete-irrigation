"""Schedule-advice validation rail (v1.56) — pure, HA-free."""

from __future__ import annotations

from custom_components.complete_irrigation.schedule_advisor import validate_schedule_advice

SCHEDS = {
    "grass": {"duration_minutes": 30, "min_chunk": 5, "name": "Grass"},
    "bath": {"duration_minutes": 15, "min_chunk": 5, "name": "Bird Bath"},
}


def test_valid_shift():
    raw = {
        "items": [
            {
                "type": "shift",
                "schedule_id": "grass",
                "proposed_start": "05:30",
                "reason": "make room",
            }
        ]
    }
    out = validate_schedule_advice(raw, schedules_by_id=SCHEDS)
    assert out["items"][0]["proposed_start"] == "05:30"
    assert out["items"][0]["schedule_name"] == "Grass"


def test_valid_split_sums_to_duration():
    raw = {
        "items": [
            {
                "type": "split",
                "schedule_id": "bath",
                "parts": [{"start": "05:30", "minutes": 5}, {"start": "06:40", "minutes": 10}],
            }
        ]
    }
    out = validate_schedule_advice(raw, schedules_by_id=SCHEDS)
    assert len(out["items"][0]["parts"]) == 2


def test_split_rejected_when_sum_mismatches_duration():
    # 5 + 5 = 10 != 15 -> would lose water -> dropped
    raw = {
        "items": [
            {
                "type": "split",
                "schedule_id": "bath",
                "parts": [{"start": "05:30", "minutes": 5}, {"start": "06:40", "minutes": 5}],
            }
        ]
    }
    assert validate_schedule_advice(raw, schedules_by_id=SCHEDS) is None


def test_split_rejected_when_part_below_floor():
    # a 2-min part is below the 5-min floor
    raw = {
        "items": [
            {
                "type": "split",
                "schedule_id": "bath",
                "parts": [{"start": "05:30", "minutes": 2}, {"start": "06:40", "minutes": 13}],
            }
        ]
    }
    assert validate_schedule_advice(raw, schedules_by_id=SCHEDS) is None


def test_unknown_schedule_dropped():
    raw = {"items": [{"type": "shift", "schedule_id": "ghost", "proposed_start": "05:30"}]}
    assert validate_schedule_advice(raw, schedules_by_id=SCHEDS) is None


def test_bad_start_time_dropped():
    raw = {"items": [{"type": "shift", "schedule_id": "grass", "proposed_start": "25:99"}]}
    assert validate_schedule_advice(raw, schedules_by_id=SCHEDS) is None


def test_mixed_items_keep_only_valid():
    raw = {
        "items": [
            {"type": "shift", "schedule_id": "grass", "proposed_start": "05:30"},
            {"type": "shift", "schedule_id": "ghost", "proposed_start": "06:00"},  # dropped
            {"type": "bogus", "schedule_id": "bath"},  # dropped
        ],
        "summary": "one move",
    }
    out = validate_schedule_advice(raw, schedules_by_id=SCHEDS)
    assert len(out["items"]) == 1 and out["summary"] == "one move"


def test_garbage_returns_none():
    for bad in (None, {}, "x", {"items": "nope"}, {"items": []}):
        assert validate_schedule_advice(bad, schedules_by_id=SCHEDS) is None
