"""Tests for plant_design.build_yard_report (pure logic, v2).

Deriving a loop's runtime + weekly frequency from the schedules that water
its zone, then running the hydraulics core per zone.
"""

from __future__ import annotations

from datetime import date, time

from custom_components.complete_irrigation.plant import PlantRecord
from custom_components.complete_irrigation.plant_design import (
    build_yard_report,
    schedule_runs_per_week,
    serialize_loop_report,
    watering_schedules_for_zone,
    zone_run_minutes,
)
from custom_components.complete_irrigation.schedule import (
    MODE_INTERVAL,
    MODE_INTERVAL_HOURS,
    Schedule,
)


def _weekday_sched(zone, dur, weekdays=(0, 1, 2, 3, 4, 5, 6), sid="w", name="W", enabled=True):
    return Schedule(
        id=sid,
        name=name,
        zone_entity_id=zone,
        start_time=time(6, 0),
        duration_minutes=dur,
        weekdays=tuple(weekdays),
        enabled=enabled,
    )


def _interval_sched(zone, dur, days, sid="i", name="I", enabled=True):
    return Schedule(
        id=sid,
        name=name,
        zone_entity_id=zone,
        start_time=time(3, 0),
        duration_minutes=dur,
        weekdays=(),
        mode=MODE_INTERVAL,
        interval_days=days,
        interval_anchor=date(2026, 6, 1),
        enabled=enabled,
    )


def _plant(pid, zone, cat="moderate", area=30.0, name=None):
    return PlantRecord(
        id=pid,
        name=name or f"plant-{pid}",
        wucols_category=cat,
        canopy_area_sqft=area,
        zone_entity_id=zone,
    )


# ── runs-per-week derivation ────────────────────────────────────────


def test_runs_per_week_weekdays():
    assert schedule_runs_per_week(_weekday_sched("switch.z", 20, weekdays=(0, 1, 2, 3, 4))) == 5.0


def test_runs_per_week_interval():
    assert schedule_runs_per_week(_interval_sched("switch.z", 135, days=2)) == 3.5


def test_runs_per_week_interval_hours_continuous():
    s = Schedule(
        id="h",
        name="H",
        zone_entity_id="switch.bird",
        start_time=time(6, 0),
        duration_minutes=15,
        weekdays=(),
        mode=MODE_INTERVAL_HOURS,
        interval_hours=6,
        interval_anchor=date(2026, 6, 1),
    )
    assert schedule_runs_per_week(s) == 28.0  # 24/6 = 4 per day x 7


# ── zone run minutes ────────────────────────────────────────────────


def test_zone_run_minutes_matches_and_misses():
    s = _weekday_sched("switch.citrus", 240)
    assert zone_run_minutes(s, "switch.citrus") == 240
    assert zone_run_minutes(s, "switch.other") == 0


# ── yard report ─────────────────────────────────────────────────────


def test_report_uses_schedule_runtime_and_frequency():
    plants = [_plant("p1", "switch.citrus", cat="high", area=100)]
    scheds = [_weekday_sched("switch.citrus", 240)]
    reports = build_yard_report(plants, scheds, eto_in_week=1.8)
    assert len(reports) == 1
    rep = reports[0]
    assert rep.loop.id == "switch.citrus"
    assert rep.loop.runtime_minutes == 240
    assert rep.loop.runs_per_week == 7.0
    assert len(rep.plants) == 1
    assert rep.plants[0].status in ("OK", "UNDER", "OVER")


def test_zone_with_no_schedule_is_flagged():
    plants = [_plant("p1", "switch.orphan", cat="high")]
    reports = build_yard_report(plants, schedules=[], eto_in_week=1.8)
    assert len(reports) == 1
    assert any("no enabled schedule" in w.lower() for w in reports[0].warnings)


def test_disabled_schedule_does_not_count():
    plants = [_plant("p1", "switch.citrus")]
    scheds = [_weekday_sched("switch.citrus", 240, enabled=False)]
    reports = build_yard_report(plants, scheds, eto_in_week=1.8)
    assert any("no enabled schedule" in w.lower() for w in reports[0].warnings)


def test_multiple_schedules_picks_primary_and_lists_all():
    # A big weekly soak (240m x7 = 1680 wk-min) vs a small interval (10m x3.5
    # = 35 wk-min). Primary = the soak; ALL schedules are LISTED (not warned).
    big = _weekday_sched("switch.citrus", 240, sid="big", name="Big Soak")
    small = _interval_sched("switch.citrus", 10, days=2, sid="sm", name="Top-up")
    reports = build_yard_report([_plant("p1", "switch.citrus")], [small, big], eto_in_week=1.8)
    rep = reports[0]
    assert rep.loop.runtime_minutes == 240  # the soak, not the top-up
    # v1.40.7 — no "also watered by" WARNING; instead both schedules are listed,
    # most-weekly-water first, with the soak marked primary.
    assert not any("also watered" in w.lower() for w in rep.warnings)
    assert [s["name"] for s in rep.schedules] == ["Big Soak", "Top-up"]
    assert rep.schedules[0]["primary"] is True
    assert rep.schedules[1]["primary"] is False
    assert rep.schedules[0]["runtime_minutes"] == 240
    # serialization carries the list to the panel
    from custom_components.complete_irrigation.plant_design import serialize_loop_report

    assert [s["name"] for s in serialize_loop_report(rep)["schedules"]] == ["Big Soak", "Top-up"]


def test_watering_schedules_sorted_by_weekly_water():
    big = _weekday_sched("switch.z", 240, sid="big")
    small = _interval_sched("switch.z", 10, days=2, sid="sm")
    ordered = watering_schedules_for_zone([small, big], "switch.z")
    assert [s.id for s, _, _ in ordered] == ["big", "sm"]


# ── serialization (ws_api / panel) ──────────────────────────────────


def test_serialize_loop_report_is_json_safe():
    import json

    plants = [_plant("p1", "switch.citrus", cat="high", area=100)]
    scheds = [_weekday_sched("switch.citrus", 240)]
    rep = build_yard_report(plants, scheds, eto_in_week=1.8)[0]
    d = serialize_loop_report(rep)
    json.dumps(d)  # must not raise (no inf/tuples/dataclasses)
    assert d["zone_entity_id"] == "switch.citrus"
    assert d["plants"][0]["status"] in ("OK", "UNDER", "OVER")
    assert isinstance(d["plants"][0]["emitters"], list)


def test_serialize_handles_infinite_required_gph():
    import json
    import math

    # A zone with no schedule -> required_gph is inf -> must serialize to None.
    rep = build_yard_report([_plant("p1", "switch.orphan")], [], eto_in_week=1.8)[0]
    d = serialize_loop_report(rep)
    json.dumps(d)
    assert all(
        not (isinstance(r["required_gph"], float) and math.isinf(r["required_gph"]))
        for r in d["plants"]
    )
    assert d["plants"][0]["required_gph"] is None
