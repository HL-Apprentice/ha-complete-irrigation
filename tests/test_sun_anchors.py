"""v1.40 — sun-anchored schedules: sunrise/sunset ± offset, finish anchor,
fixed-time fallback. Pure planner tests with a fake sun-times provider."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest

from custom_components.complete_irrigation.run_planner import next_runs
from custom_components.complete_irrigation.schedule import Schedule, ZoneStep

DAY = date(2026, 7, 13)  # a Monday


def _sun(day):
    # Fake provider: sunrise 05:30, sunset 19:45, every day.
    return (
        datetime.combine(day, time(5, 30)),
        datetime.combine(day, time(19, 45)),
    )


def _sched(**kw):
    base = dict(
        id="s1",
        name="Sun test",
        zone_entity_id="switch.z1",
        start_time=time(4, 0),  # fallback
        duration_minutes=30,
        weekdays=(0, 1, 2, 3, 4, 5, 6),
    )
    base.update(kw)
    return Schedule(**base)


def _runs(sched, sun=_sun):
    frm = datetime.combine(DAY, time(0, 0))
    return next_runs([sched], frm, frm + timedelta(days=1), sun_times=sun)


def test_sunrise_start_anchor():
    runs = _runs(_sched(sun_event="sunrise"))
    assert len(runs) == 1
    assert runs[0].start_at == datetime.combine(DAY, time(5, 30))


def test_sunset_with_negative_offset():
    runs = _runs(_sched(sun_event="sunset", sun_offset_minutes=-45))
    assert runs[0].start_at == datetime.combine(DAY, time(19, 0))


def test_finish_at_sunrise_backs_up_by_duration():
    runs = _runs(_sched(sun_event="sunrise", anchor="finish"))
    # 30-min run finishing at 05:30 -> starts 05:00
    assert runs[0].start_at == datetime.combine(DAY, time(5, 0))
    end = runs[0].start_at + timedelta(minutes=runs[0].duration_minutes)
    assert end == datetime.combine(DAY, time(5, 30))


def test_finish_anchor_multi_zone_accounts_for_full_span():
    sched = _sched(
        sun_event="sunrise",
        anchor="finish",
        zone_steps=(
            ZoneStep("switch.z1", 30),
            ZoneStep("switch.z2", 20),
        ),
    )
    runs = _runs(sched)
    assert len(runs) == 2
    last_end = runs[-1].start_at + timedelta(minutes=runs[-1].duration_minutes)
    # The WHOLE firing (both steps + buffer) completes at sunrise.
    assert last_end == datetime.combine(DAY, time(5, 30))
    assert runs[0].start_at < runs[1].start_at


def test_no_provider_falls_back_to_fixed_time():
    runs = _runs(_sched(sun_event="sunrise"), sun=None)
    assert runs[0].start_at == datetime.combine(DAY, time(4, 0))


def test_provider_none_or_raising_falls_back():
    runs = _runs(_sched(sun_event="sunrise"), sun=lambda d: None)
    assert runs[0].start_at == datetime.combine(DAY, time(4, 0))

    def boom(d):
        raise RuntimeError("astral exploded")

    runs = _runs(_sched(sun_event="sunrise"), sun=boom)
    assert runs[0].start_at == datetime.combine(DAY, time(4, 0))


def test_plain_schedule_unaffected_by_provider():
    runs = _runs(_sched())
    assert runs[0].start_at == datetime.combine(DAY, time(4, 0))


def test_validation():
    with pytest.raises(ValueError):
        _sched(sun_event="noon")
    with pytest.raises(ValueError):
        _sched(sun_event="sunrise", sun_offset_minutes=999)
    with pytest.raises(ValueError):
        _sched(anchor="middle")
    with pytest.raises(ValueError):
        Schedule(
            id="s2",
            name="x",
            zone_entity_id="switch.z",
            start_time=time(4, 0),
            duration_minutes=10,
            mode="interval_hours",
            interval_hours=6,
            interval_anchor=DAY,
            sun_event="sunrise",
        )


def test_roundtrip_and_tolerant_load():
    s = _sched(sun_event="sunset", sun_offset_minutes=-30, anchor="finish")
    d = s.to_dict()
    s2 = Schedule.from_dict(d)
    assert (s2.sun_event, s2.sun_offset_minutes, s2.anchor) == ("sunset", -30, "finish")
    d["sun_event"] = "eclipse"  # junk degrades to None, not a crash
    d["anchor"] = "sideways"
    s3 = Schedule.from_dict(d)
    assert s3.sun_event is None and s3.anchor == "start"
    # pre-v1.40 record (no keys at all)
    for k in ("sun_event", "sun_offset_minutes", "anchor"):
        d.pop(k, None)
    s4 = Schedule.from_dict(d)
    assert s4.sun_event is None and s4.sun_offset_minutes == 0 and s4.anchor == "start"
