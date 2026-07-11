"""Tests for Schedule + ScheduleStore (pure logic, no HA).

A Schedule is a user-defined rule: "run this zone at this time, this
long, on these weekdays". The ScheduleStore is the CRUD layer that
holds them.
"""

from __future__ import annotations

from datetime import time

import pytest

from custom_components.complete_irrigation.schedule import Schedule, ScheduleStore

# ════════════════════════════════════════════════════════════════════
# Schedule construction + validation
# ════════════════════════════════════════════════════════════════════


def _good_schedule(**overrides) -> Schedule:
    defaults = {
        "id": "s1",
        "name": "Morning Front",
        "zone_entity_id": "switch.front_lawn",
        "start_time": time(6, 0),
        "duration_minutes": 15,
        "weekdays": (0, 1, 2, 3, 4),  # Mon-Fri
    }
    defaults.update(overrides)
    return Schedule(**defaults)


def test_schedule_constructs_with_valid_fields():
    s = _good_schedule()
    assert s.id == "s1"
    assert s.name == "Morning Front"
    assert s.zone_entity_id == "switch.front_lawn"
    assert s.start_time == time(6, 0)
    assert s.duration_minutes == 15
    assert s.weekdays == (0, 1, 2, 3, 4)
    assert s.enabled is True  # default


def test_schedule_is_frozen():
    """Modifying a Schedule should raise (frozen dataclass)."""
    s = _good_schedule()
    with pytest.raises((AttributeError, TypeError)):
        s.name = "different"  # type: ignore[misc]


def test_schedule_rejects_empty_name():
    with pytest.raises(ValueError, match="name"):
        _good_schedule(name="")
    with pytest.raises(ValueError, match="name"):
        _good_schedule(name="   ")


def test_schedule_rejects_empty_zone():
    with pytest.raises(ValueError, match="zone"):
        _good_schedule(zone_entity_id="")


def test_schedule_rejects_non_positive_duration():
    with pytest.raises(ValueError, match="duration"):
        _good_schedule(duration_minutes=0)
    with pytest.raises(ValueError, match="duration"):
        _good_schedule(duration_minutes=-5)


def test_schedule_rejects_empty_weekdays():
    with pytest.raises(ValueError, match="weekdays"):
        _good_schedule(weekdays=())


def test_schedule_rejects_invalid_weekday_values():
    """Weekdays must be in 0..6 (Mon..Sun)."""
    with pytest.raises(ValueError, match="weekdays"):
        _good_schedule(weekdays=(7,))
    with pytest.raises(ValueError, match="weekdays"):
        _good_schedule(weekdays=(-1, 0))


def test_schedule_disabled_when_specified():
    s = _good_schedule(enabled=False)
    assert s.enabled is False


# ════════════════════════════════════════════════════════════════════
# Schedule serialization round-trip
# ════════════════════════════════════════════════════════════════════


def test_schedule_to_dict_has_expected_shape():
    s = _good_schedule()
    d = s.to_dict()
    assert d == {
        "id": "s1",
        "name": "Morning Front",
        "zone_entity_id": "switch.front_lawn",
        "start_time": "06:00",
        "duration_minutes": 15,
        "weekdays": [0, 1, 2, 3, 4],
        "enabled": True,
        "end_date": None,
        "mode": "weekdays",
        "interval_days": None,
        "interval_anchor": None,
        "interval_hours": None,
        "interval_end_time": None,
        "start_date": None,
        "repeat_annually": False,
        "zone_steps": [],
        "created_via": "panel",
        "ignore_wind": False,
        "ignore_hot_weather": False,
        "ignore_rain_lockout": False,
        "color": None,
        "sun_event": None,
        "sun_offset_minutes": 0,
        "anchor": "start",
    }


def test_schedule_with_end_date_roundtrip():
    """Establishment-mode schedules carry an end_date that survives serialization."""
    from datetime import date

    s = _good_schedule(end_date=date(2026, 6, 1))
    d = s.to_dict()
    assert d["end_date"] == "2026-06-01"
    s2 = Schedule.from_dict(d)
    assert s2.end_date == date(2026, 6, 1)
    assert s2 == s


def test_schedule_from_dict_reconstructs_original():
    s = _good_schedule(start_time=time(18, 45), enabled=False)
    d = s.to_dict()
    s2 = Schedule.from_dict(d)
    assert s2 == s


def test_schedule_from_dict_handles_missing_enabled_as_true():
    """Backward compat: older stored schedules may lack the enabled flag."""
    d = _good_schedule().to_dict()
    d.pop("enabled")
    s = Schedule.from_dict(d)
    assert s.enabled is True


# ── interval mode (v1.4) ─────────────────────────────────────────


def test_schedule_interval_mode_constructs():
    from datetime import date

    s = Schedule(
        id="tree1",
        name="Tree deep watering",
        zone_entity_id="switch.trees",
        start_time=time(2, 0),
        duration_minutes=300,
        weekdays=(),  # not used in interval mode
        mode="interval",
        interval_days=5,
        interval_anchor=date(2026, 5, 20),
    )
    assert s.mode == "interval"
    assert s.interval_days == 5


def test_schedule_interval_mode_requires_interval_days():
    from datetime import date

    with pytest.raises(ValueError, match="interval_days"):
        Schedule(
            id="s1",
            name="Trees",
            zone_entity_id="switch.trees",
            start_time=time(2, 0),
            duration_minutes=300,
            weekdays=(),
            mode="interval",
            interval_anchor=date(2026, 5, 20),
        )


def test_schedule_interval_mode_requires_anchor():
    with pytest.raises(ValueError, match="interval_anchor"):
        Schedule(
            id="s1",
            name="Trees",
            zone_entity_id="switch.trees",
            start_time=time(2, 0),
            duration_minutes=300,
            weekdays=(),
            mode="interval",
            interval_days=5,
        )


def test_schedule_interval_mode_roundtrip():
    from datetime import date

    original = Schedule(
        id="tree1",
        name="Tree deep watering",
        zone_entity_id="switch.trees",
        start_time=time(2, 0),
        duration_minutes=300,
        weekdays=(),
        mode="interval",
        interval_days=5,
        interval_anchor=date(2026, 5, 20),
    )
    rebuilt = Schedule.from_dict(original.to_dict())
    assert rebuilt == original


def test_schedule_pre_v14_dict_without_mode_defaults_to_weekdays():
    """Backward compat: a schedule stored before v1.4 has no `mode` key."""
    old_dict = {
        "id": "old1",
        "name": "Old schedule",
        "zone_entity_id": "switch.lawn",
        "start_time": "06:00",
        "duration_minutes": 15,
        "weekdays": [0, 2, 4],
        "enabled": True,
        # NOTE: no mode, no interval_*
    }
    s = Schedule.from_dict(old_dict)
    assert s.mode == "weekdays"
    assert s.weekdays == (0, 2, 4)


# ════════════════════════════════════════════════════════════════════
# ScheduleStore CRUD
# ════════════════════════════════════════════════════════════════════


def test_store_starts_empty():
    store = ScheduleStore()
    assert store.all() == []
    assert store.enabled_schedules() == []


def test_store_add_then_get():
    store = ScheduleStore()
    s = _good_schedule()
    store.add(s)
    assert store.get("s1") == s
    assert store.all() == [s]


def test_store_add_rejects_duplicate_id():
    store = ScheduleStore()
    store.add(_good_schedule())
    with pytest.raises(KeyError, match="s1"):
        store.add(_good_schedule(name="different name"))


def test_store_upsert_overwrites_existing():
    store = ScheduleStore()
    store.add(_good_schedule())
    new = _good_schedule(name="Renamed")
    store.upsert(new)
    assert store.get("s1") == new


def test_store_upsert_inserts_if_missing():
    store = ScheduleStore()
    s = _good_schedule()
    store.upsert(s)
    assert store.get("s1") == s


def test_store_delete_existing_returns_true_and_removes():
    store = ScheduleStore()
    store.add(_good_schedule())
    assert store.delete("s1") is True
    assert store.get("s1") is None
    assert store.all() == []


def test_store_delete_missing_returns_false():
    store = ScheduleStore()
    assert store.delete("nope") is False


def test_store_get_missing_returns_none():
    store = ScheduleStore()
    assert store.get("nope") is None


def test_store_all_returns_in_insertion_order():
    store = ScheduleStore()
    s1 = _good_schedule(id="s1", name="A")
    s2 = _good_schedule(id="s2", name="B")
    s3 = _good_schedule(id="s3", name="C")
    store.add(s1)
    store.add(s2)
    store.add(s3)
    assert [s.id for s in store.all()] == ["s1", "s2", "s3"]


def test_store_enabled_schedules_filters_correctly():
    store = ScheduleStore()
    on_schedule = _good_schedule(id="on", name="Active", enabled=True)
    off_schedule = _good_schedule(id="off", name="Disabled", enabled=False)
    store.add(on_schedule)
    store.add(off_schedule)
    assert store.enabled_schedules() == [on_schedule]
    assert set(store.all()) == {on_schedule, off_schedule}


# ════════════════════════════════════════════════════════════════════
# ScheduleStore serialization round-trip
# ════════════════════════════════════════════════════════════════════


def test_store_to_serializable_lists_all_in_order():
    store = ScheduleStore()
    store.add(_good_schedule(id="s1"))
    store.add(_good_schedule(id="s2", start_time=time(18, 0)))
    data = store.to_serializable()
    assert isinstance(data, list)
    assert len(data) == 2
    assert data[0]["id"] == "s1"
    assert data[1]["id"] == "s2"
    assert data[1]["start_time"] == "18:00"


def test_store_from_serializable_reconstructs_store():
    original = ScheduleStore()
    original.add(_good_schedule(id="s1"))
    original.add(_good_schedule(id="s2", name="Evening", enabled=False))

    blob = original.to_serializable()
    rebuilt = ScheduleStore.from_serializable(blob)

    assert {s.id for s in rebuilt.all()} == {"s1", "s2"}
    assert rebuilt.get("s2").enabled is False
    assert rebuilt.get("s2").name == "Evening"


def test_store_from_serializable_with_empty_list_returns_empty_store():
    store = ScheduleStore.from_serializable([])
    assert store.all() == []


# ════════════════════════════════════════════════════════════════════
# Multi-zone schedules (v1.8) — ZoneStep + Schedule.zone_steps
# ════════════════════════════════════════════════════════════════════


def test_zone_step_constructs_and_rejects_invalid():
    from custom_components.complete_irrigation.schedule import ZoneStep

    s = ZoneStep(zone_entity_id="switch.a", duration_minutes=10)
    assert s.zone_entity_id == "switch.a"
    assert s.duration_minutes == 10

    with pytest.raises(ValueError, match="zone_entity_id"):
        ZoneStep(zone_entity_id="", duration_minutes=10)
    with pytest.raises(ValueError, match="duration_minutes"):
        ZoneStep(zone_entity_id="switch.a", duration_minutes=0)


def test_zone_step_roundtrip():
    from custom_components.complete_irrigation.schedule import ZoneStep

    s = ZoneStep(zone_entity_id="switch.a", duration_minutes=12)
    assert ZoneStep.from_dict(s.to_dict()) == s


def test_schedule_default_has_empty_zone_steps():
    s = _good_schedule()
    assert s.zone_steps == ()


def test_schedule_all_steps_falls_back_to_single_step():
    """A schedule with no zone_steps should expose a single derived
    step from the top-level zone_entity_id + duration_minutes."""
    s = _good_schedule()
    steps = s.all_steps()
    assert len(steps) == 1
    assert steps[0].zone_entity_id == "switch.front_lawn"
    assert steps[0].duration_minutes == 15


def test_schedule_with_zone_steps_returns_them_verbatim():
    from custom_components.complete_irrigation.schedule import ZoneStep

    steps = (
        ZoneStep("switch.front_lawn", 15),
        ZoneStep("switch.back_lawn", 10),
        ZoneStep("switch.garden", 8),
    )
    s = _good_schedule(zone_steps=steps)
    assert s.all_steps() == steps
    # Top-level still mirrors first step
    assert s.zone_entity_id == "switch.front_lawn"
    assert s.duration_minutes == 15


def test_schedule_zone_steps_must_match_top_level_first():
    from custom_components.complete_irrigation.schedule import ZoneStep

    # First step's zone must match top-level
    with pytest.raises(ValueError, match="zone_entity_id"):
        _good_schedule(
            zone_steps=(ZoneStep("switch.different", 15),),
        )
    # First step's duration must match top-level
    with pytest.raises(ValueError, match="duration_minutes"):
        _good_schedule(
            zone_steps=(ZoneStep("switch.front_lawn", 99),),
        )


def test_schedule_multi_zone_roundtrip():
    from custom_components.complete_irrigation.schedule import ZoneStep

    original = _good_schedule(
        zone_steps=(
            ZoneStep("switch.front_lawn", 15),
            ZoneStep("switch.back_lawn", 10),
        )
    )
    rebuilt = Schedule.from_dict(original.to_dict())
    assert rebuilt == original
    assert len(rebuilt.zone_steps) == 2


def test_schedule_pre_v18_dict_without_zone_steps_works():
    """Existing schedules saved before v1.8 have no zone_steps key."""
    old_dict = {
        "id": "old1",
        "name": "Pre-v1.8 schedule",
        "zone_entity_id": "switch.lawn",
        "start_time": "06:00",
        "duration_minutes": 15,
        "weekdays": [0, 2, 4],
        "enabled": True,
        "mode": "weekdays",
    }
    s = Schedule.from_dict(old_dict)
    assert s.zone_steps == ()
    assert s.all_steps()[0].zone_entity_id == "switch.lawn"


# ════════════════════════════════════════════════════════════════════
# Interval-hours mode (v1.11)
# ════════════════════════════════════════════════════════════════════


def test_schedule_interval_hours_mode_constructs():
    from datetime import date

    s = Schedule(
        id="hourly",
        name="Hot day cycles",
        zone_entity_id="switch.veg",
        start_time=time(6, 0),
        duration_minutes=10,
        weekdays=(),
        mode="interval_hours",
        interval_hours=6,
        interval_anchor=date(2026, 5, 22),
    )
    assert s.mode == "interval_hours"
    assert s.interval_hours == 6


def test_schedule_interval_hours_requires_hours():
    from datetime import date

    with pytest.raises(ValueError, match="interval_hours"):
        Schedule(
            id="s",
            name="x",
            zone_entity_id="switch.x",
            start_time=time(6, 0),
            duration_minutes=10,
            weekdays=(),
            mode="interval_hours",
            interval_anchor=date(2026, 5, 22),
        )


def test_schedule_interval_hours_requires_anchor():
    with pytest.raises(ValueError, match="interval_anchor"):
        Schedule(
            id="s",
            name="x",
            zone_entity_id="switch.x",
            start_time=time(6, 0),
            duration_minutes=10,
            weekdays=(),
            mode="interval_hours",
            interval_hours=6,
        )


def test_schedule_interval_hours_roundtrip():
    from datetime import date

    original = Schedule(
        id="hourly",
        name="Hot day cycles",
        zone_entity_id="switch.veg",
        start_time=time(6, 0),
        duration_minutes=10,
        weekdays=(),
        mode="interval_hours",
        interval_hours=4,
        interval_anchor=date(2026, 5, 22),
    )
    rebuilt = Schedule.from_dict(original.to_dict())
    assert rebuilt == original


# ── interval_hours daily-window field (v1.14.1) ────────────────────


def test_schedule_interval_end_time_accepted_when_after_start_time():
    from datetime import date

    s = Schedule(
        id="hourly",
        name="Daily window",
        zone_entity_id="switch.veg",
        start_time=time(6, 0),
        duration_minutes=10,
        weekdays=(),
        mode="interval_hours",
        interval_hours=2,
        interval_anchor=date(2026, 5, 22),
        interval_end_time=time(20, 0),
    )
    assert s.interval_end_time == time(20, 0)


def test_schedule_interval_end_time_rejected_when_not_after_start_time():
    from datetime import date

    import pytest

    with pytest.raises(ValueError, match="interval_end_time must be after start_time"):
        Schedule(
            id="bad",
            name="Bad window",
            zone_entity_id="switch.veg",
            start_time=time(20, 0),
            duration_minutes=10,
            weekdays=(),
            mode="interval_hours",
            interval_hours=2,
            interval_anchor=date(2026, 5, 22),
            interval_end_time=time(6, 0),
        )


def test_schedule_interval_end_time_rejected_outside_interval_hours_mode():
    import pytest

    with pytest.raises(ValueError, match="only valid in interval_hours mode"):
        Schedule(
            id="bad",
            name="Bad",
            zone_entity_id="switch.veg",
            start_time=time(6, 0),
            duration_minutes=10,
            weekdays=(0, 1, 2),
            mode="weekdays",
            interval_end_time=time(20, 0),
        )


def test_schedule_interval_end_time_roundtrip():
    from datetime import date

    original = Schedule(
        id="hourly",
        name="Daily window",
        zone_entity_id="switch.veg",
        start_time=time(6, 0),
        duration_minutes=10,
        weekdays=(),
        mode="interval_hours",
        interval_hours=2,
        interval_anchor=date(2026, 5, 22),
        interval_end_time=time(20, 0),
    )
    rebuilt = Schedule.from_dict(original.to_dict())
    assert rebuilt == original
    assert rebuilt.interval_end_time == time(20, 0)


# ── per-schedule weather-gate opt-outs (v1.17.3) ──────────────────


def test_schedule_ignore_flags_default_false():
    s = _good_schedule()
    assert s.ignore_wind is False
    assert s.ignore_hot_weather is False
    assert s.ignore_rain_lockout is False


def test_schedule_ignore_flags_roundtrip():
    s = _good_schedule(
        ignore_wind=True,
        ignore_hot_weather=True,
        ignore_rain_lockout=True,
    )
    rebuilt = Schedule.from_dict(s.to_dict())
    assert rebuilt == s
    assert rebuilt.ignore_wind is True
    assert rebuilt.ignore_hot_weather is True
    assert rebuilt.ignore_rain_lockout is True


def test_schedule_color_defaults_none_and_roundtrips():
    # v1.18 — optional color field
    assert _good_schedule().color is None
    s = _good_schedule(color="#1e88e5")
    rebuilt = Schedule.from_dict(s.to_dict())
    assert rebuilt == s
    assert rebuilt.color == "#1e88e5"
    # with_changes preserves it
    assert s.with_changes(enabled=False).color == "#1e88e5"


def test_schedule_ignore_flags_with_changes_preserves():
    # Schedule.with_changes (dataclasses.replace) must preserve the
    # ignore_* fields when updating an unrelated field — same bug class
    # as the field-by-field rebuild we fixed in v1.15.
    s = _good_schedule(ignore_wind=True, ignore_hot_weather=True)
    updated = s.with_changes(enabled=False)
    assert updated.ignore_wind is True
    assert updated.ignore_hot_weather is True
    assert updated.enabled is False


# ════════════════════════════════════════════════════════════════════
# Active period — start_date / end_date / repeat_annually (v1.12)
# ════════════════════════════════════════════════════════════════════


def test_schedule_start_date_defaults_to_none():
    assert _good_schedule().start_date is None


def test_schedule_repeat_annually_defaults_to_false():
    assert _good_schedule().repeat_annually is False


def test_schedule_active_period_roundtrips():
    from datetime import date

    s = _good_schedule(
        start_date=date(2026, 3, 1),
        end_date=date(2026, 10, 31),
        repeat_annually=True,
    )
    rebuilt = Schedule.from_dict(s.to_dict())
    assert rebuilt == s


def test_schedule_repeat_annually_requires_both_dates():
    from datetime import date

    with pytest.raises(ValueError, match="repeat_annually"):
        _good_schedule(start_date=date(2026, 3, 1), repeat_annually=True)
    with pytest.raises(ValueError, match="repeat_annually"):
        _good_schedule(end_date=date(2026, 10, 31), repeat_annually=True)


def test_schedule_repeat_annually_rejects_year_wrap():
    """Window must stay within one calendar year (Mar→Oct OK; Nov→Feb not)."""
    from datetime import date

    with pytest.raises(ValueError, match="wrap"):
        _good_schedule(
            start_date=date(2026, 11, 15),
            end_date=date(2026, 2, 28),
            repeat_annually=True,
        )


def test_is_within_active_window_no_bounds():
    """No start/end → always within."""
    from datetime import date

    from custom_components.complete_irrigation.schedule import is_within_active_window

    s = _good_schedule()
    assert is_within_active_window(s, date(1999, 1, 1)) is True
    assert is_within_active_window(s, date(2100, 12, 31)) is True


def test_is_within_active_window_one_shot_bounds():
    from datetime import date

    from custom_components.complete_irrigation.schedule import is_within_active_window

    s = _good_schedule(start_date=date(2026, 3, 1), end_date=date(2026, 10, 31))
    assert is_within_active_window(s, date(2026, 2, 28)) is False
    assert is_within_active_window(s, date(2026, 3, 1)) is True
    assert is_within_active_window(s, date(2026, 6, 15)) is True
    assert is_within_active_window(s, date(2026, 10, 31)) is True
    assert is_within_active_window(s, date(2026, 11, 1)) is False
    # Next year — one-shot, so outside the window
    assert is_within_active_window(s, date(2027, 6, 15)) is False


def test_is_within_active_window_annual_repeats_each_year():
    from datetime import date

    from custom_components.complete_irrigation.schedule import is_within_active_window

    s = _good_schedule(
        start_date=date(2026, 3, 1),
        end_date=date(2026, 10, 31),
        repeat_annually=True,
    )
    # 2026 in-window
    assert is_within_active_window(s, date(2026, 6, 15)) is True
    # 2027 same month/day = also in window
    assert is_within_active_window(s, date(2027, 6, 15)) is True
    # 2030 December = outside (Nov+ outside)
    assert is_within_active_window(s, date(2030, 12, 25)) is False
    # On the edges
    assert is_within_active_window(s, date(2099, 3, 1)) is True
    assert is_within_active_window(s, date(2099, 10, 31)) is True
    assert is_within_active_window(s, date(2099, 2, 28)) is False
    assert is_within_active_window(s, date(2099, 11, 1)) is False
