"""Coordinator wiring for the v1.64 rain lockout.

Needs Home Assistant importable (Store, dt, the binary sensor). Skips on a
bare box, same as the other coordinator tests. The policy math itself is in
test_rain_lockout_policy.py and does not need HA.
"""

from __future__ import annotations

import copy
from datetime import date, time, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("homeassistant.util.dt")

from homeassistant.util import dt as dt_util

from custom_components.complete_irrigation.binary_sensor import RainLockoutBinarySensor
from custom_components.complete_irrigation.const import DOMAIN
from custom_components.complete_irrigation.coordinator import ScheduleCoordinator
from custom_components.complete_irrigation.rain_lockout import (
    ZONE_DEEP_DRIP,
    ZONE_GERMINATION,
    ZONE_TURF,
    StoredRainLockout,
)
from custom_components.complete_irrigation.run_history import STATUS_SKIPPED
from custom_components.complete_irrigation.schedule import Schedule
from custom_components.complete_irrigation.services import handle_extend_rain_lockout


def _coord(**config) -> ScheduleCoordinator:
    coord = ScheduleCoordinator(MagicMock(), "test-entry")
    coord._config.update(config)
    return coord


def _state(value, **attrs) -> MagicMock:
    state = MagicMock()
    state.state = value
    state.attributes = dict(attrs)
    return state


def _daily(name: str, zone: str, *, hour: int, minute: int, **overrides) -> Schedule:
    fields = {
        "id": name,
        "name": name,
        "zone_entity_id": zone,
        "start_time": time(hour, minute),
        "duration_minutes": 10,
        "weekdays": (0, 1, 2, 3, 4, 5, 6),
    }
    fields.update(overrides)
    return Schedule(**fields)


def _aware(year, month, day, hour, minute):
    return dt_util.now().replace(
        year=year, month=month, day=day, hour=hour, minute=minute, second=0, microsecond=0
    )


# ── skip logging at the gate ──────────────────────────────────────


async def test_record_skip_logs_locked_zones_and_leaves_opt_outs_alone():
    coord = _coord()
    # 2026-05-18 is a Monday. The due window is (05:59, 06:01].
    now = _aware(2026, 5, 18, 6, 1)
    last = now - timedelta(minutes=2)
    coord.schedule_store.add(_daily("lawn", "switch.front_lawn", hour=6, minute=0))
    coord.schedule_store.add(
        _daily(
            "lawn-ignore",
            "switch.front_lawn",
            hour=6,
            minute=0,
            ignore_rain_lockout=True,
        )
    )
    coord.schedule_store.add(_daily("bath", "switch.birdbath", hour=6, minute=0))
    # An in-window establishment schedule is the germination exemption.
    # The window has to cover both the simulated tick (2026-05-18) and the
    # real date classification uses. A "new grass" name with no window is turf.
    coord.schedule_store.add(
        _daily(
            "sprouts",
            "switch.germination",
            hour=6,
            minute=0,
            created_via="establishment",
            start_date=date(2026, 5, 1),
            end_date=date(2026, 12, 31),
        )
    )
    coord.schedule_store.add(_daily("newgrass", "switch.new_grass", hour=6, minute=0))
    # Every holding type looks locked. Birdbath is too, because an active-rain
    # hold sets its clock. The ignore flag and the establishment window opt out.
    later = now + timedelta(days=2)
    coord._rain_lockout = StoredRainLockout(
        until_by_type={
            ZONE_TURF: later,
            "deep_drip": later,
            "flowers_garden": later,
            "birdbath": later,
            ZONE_GERMINATION: later,
        }
    )
    await coord._record_lockout_skips(coord.schedule_store.enabled_schedules(), last, now, None)
    skipped = [rec for rec in coord.run_history.all() if rec.status == STATUS_SKIPPED]
    zones = sorted(rec.zone_entity_id for rec in skipped)
    assert zones == ["switch.birdbath", "switch.front_lawn", "switch.new_grass"]
    assert all(rec.reason == "rain lockout" for rec in skipped)
    assert all(rec.triggers["rain_lockout"]["zone_type"] for rec in skipped)
    # The ignored lawn schedule and the in-window germination zone produced no row.
    assert len(skipped) == 3


# ── completed run, moisture direction, forecast glue ──────────────


async def test_completed_run_releases_only_that_zone_type():
    coord = _coord()
    coord._config["zones"] = {
        "switch.front_lawn": {"category": "lawn"},
        "switch.trees": {"category": "trees"},
    }
    later = dt_util.now() + timedelta(days=2)
    deep = dt_util.now() + timedelta(days=4)
    coord._rain_lockout = StoredRainLockout(
        until_by_type={ZONE_TURF: later, ZONE_DEEP_DRIP: deep},
        last_event_rain_in=0.4,
    )
    await coord.note_zone_run_completed(
        "switch.front_lawn",
        {"source": "scheduled", "schedule_id": "sched-lawn"},
    )
    # No schedule with that id, so a missing schedule does not release.
    assert "switch.front_lawn" not in coord._rain_lockout.released_zones
    coord.schedule_store.add(_daily("sched-lawn", "switch.front_lawn", hour=6, minute=0))
    await coord.note_zone_run_completed(
        "switch.front_lawn",
        {"source": "scheduled", "schedule_id": "sched-lawn"},
    )
    assert coord._rain_lockout.released_zones["switch.front_lawn"] == ZONE_TURF
    # The type clock stays up for every other turf zone.
    assert coord._rain_lockout.until_by_type[ZONE_TURF] == later
    assert coord.is_zone_locked_out("switch.front_lawn") is False
    assert coord._rain_lockout.until_by_type[ZONE_DEEP_DRIP] == deep
    assert coord._rain_lockout.last_event_rain_in == pytest.approx(0.4)
    # Manual, ignore-rain, and restart recovery do not release.
    await coord.note_zone_run_completed("switch.trees", {"source": "manual"})
    assert "switch.trees" not in coord._rain_lockout.released_zones
    ignored = _daily("sched-trees", "switch.trees", hour=5, minute=0, ignore_rain_lockout=True)
    coord.schedule_store.add(ignored)
    await coord.note_zone_run_completed(
        "switch.trees",
        {"source": "scheduled", "schedule_id": "sched-trees"},
    )
    assert "switch.trees" not in coord._rain_lockout.released_zones
    await coord.note_zone_run_completed(
        "switch.trees",
        {"source": "scheduled", "schedule_id": "sched-lawn", "resumed_after_restart": True},
    )
    assert "switch.trees" not in coord._rain_lockout.released_zones
    await coord.note_zone_run_completed(
        "switch.trees",
        {
            "source": "scheduled",
            "schedule_id": "sched-lawn",
            "triggers": {"recovered_from": "hist-1", "recovery_kind": "missed run"},
        },
    )
    assert "switch.trees" not in coord._rain_lockout.released_zones
    await coord.note_zone_run_completed(
        "switch.trees",
        {
            "source": "scheduled",
            "schedule_id": "sched-lawn",
            "triggers": {"recovery_kind": "remainder"},
        },
    )
    assert "switch.trees" not in coord._rain_lockout.released_zones


async def test_moisture_releases_a_dry_zone_and_does_not_extend_a_wet_one():
    now = dt_util.now()
    turf_until = now + timedelta(days=2)
    deep_until = now + timedelta(days=3)

    def _run(moisture: str) -> ScheduleCoordinator:
        coord = _coord(rain_sensor="sensor.rain")
        coord._config["zones"] = {
            "switch.front_lawn": {
                "category": "lawn",
                "moisture_entities": ["sensor.lawn_moist"],
                "min_pct": 21,
            },
            "switch.trees": {"category": "trees"},
        }
        states = {
            "sensor.rain": _state("0.40", unit_of_measurement="in"),
            "sensor.lawn_moist": _state(moisture),
        }
        coord._hass.states.get.side_effect = lambda eid: states.get(eid)
        coord._hass.states.async_entity_ids.return_value = []
        coord.eto_status = lambda: {"eto_in_week": 0.84}  # 0.12 in/day -> scale 1
        coord._read_heat_signal_f = lambda: 70.0  # mild, 48h ceiling
        coord._notify_lockout_change = lambda: None

        async def _no_history(*_args, **_kwargs):
            return None

        coord._tempest_history_inches = _no_history
        coord._open_meteo_for_decision = lambda _now: (None, "unavailable")
        coord._rain_lockout = StoredRainLockout(
            until_by_type={ZONE_TURF: turf_until, ZONE_DEEP_DRIP: deep_until},
            last_event_rain_in=0.40,
        )
        return coord

    dry = _run("10")
    await dry._evaluate_lockout(now)
    assert dry._rain_lockout.until_by_type[ZONE_TURF] == turf_until
    assert dry._rain_lockout.released_zones.get("switch.front_lawn") == ZONE_TURF
    assert dry.is_zone_locked_out("switch.front_lawn", now) is False
    assert dry._rain_lockout.until_by_type[ZONE_DEEP_DRIP] == deep_until

    wet = _run("80")
    await wet._evaluate_lockout(now)
    assert wet._rain_lockout.until_by_type[ZONE_TURF] == turf_until
    assert wet._rain_lockout.until_by_type[ZONE_DEEP_DRIP] == deep_until


async def test_forecast_extension_is_applied_by_the_coordinator_once():
    coord = _coord()
    now = dt_util.now()
    coord._hass.states.async_entity_ids.return_value = []
    coord._read_heat_signal_f = lambda: 70.0  # 48h ceiling
    coord._notify_lockout_change = lambda: None
    coord._rain_lockout = StoredRainLockout(
        until_by_type={ZONE_TURF: now + timedelta(hours=12)},
        last_event_rain_in=0.30,
    )

    async def _no_history(*_args, **_kwargs):
        return None

    coord._tempest_history_inches = _no_history
    coord._open_meteo_rain = {
        "past_72h_in": 0.30,
        "forecast_24h_in": 0.40,
        "forecast_24h_probability_pct": 70,
        "forecast_daily_eto_in": 0.12,
        "current_precip_mm": 0.0,
    }
    coord._open_meteo_rain_at = now
    coord._om_unconsumed_fresh = True
    await coord._evaluate_lockout(now)
    assert coord._rain_lockout.until_by_type[ZONE_TURF] == now + timedelta(hours=36)
    await coord._evaluate_lockout(now + timedelta(minutes=1))
    assert coord._rain_lockout.until_by_type[ZONE_TURF] == now + timedelta(hours=36)


def test_germination_follows_the_window_and_birdbath_needs_a_clock():
    coord = _coord(independent_zones=["switch.pots"])
    later = dt_util.now() + timedelta(hours=5)
    coord._rain_lockout = StoredRainLockout(
        until_by_type={ZONE_GERMINATION: later, "birdbath": None, ZONE_TURF: later}
    )
    # A germination or new-grass name is turf unless an establishment window
    # or an explicit override says otherwise. independent_zones is not birdbath.
    assert coord.zone_lockout_type("switch.germination_bed") == ZONE_TURF
    assert coord.is_zone_locked_out("switch.germination_bed") is True
    assert coord.zone_lockout_type("switch.new_grass") == ZONE_TURF
    assert coord.zone_lockout_type("switch.pots") == ZONE_TURF
    assert coord.zone_lockout_type("switch.flower_garden") == "flowers_garden"
    coord._config["zones"] = {
        "switch.germination_bed": {"lockout_zone_type": ZONE_GERMINATION},
    }
    assert coord.zone_lockout_type("switch.germination_bed") == ZONE_GERMINATION
    assert coord.is_zone_locked_out("switch.germination_bed") is False
    today = dt_util.now().date()
    coord.schedule_store.add(
        _daily(
            "sprouts",
            "switch.sprouts",
            hour=6,
            minute=0,
            created_via="establishment",
            start_date=today - timedelta(days=1),
            end_date=today + timedelta(days=5),
        )
    )
    assert coord.zone_lockout_type("switch.sprouts") == ZONE_GERMINATION
    assert coord.is_zone_locked_out("switch.sprouts") is False
    coord.schedule_store.add(
        _daily(
            "old-seed",
            "switch.old_seed",
            hour=6,
            minute=0,
            created_via="establishment",
            start_date=today - timedelta(days=30),
            end_date=today - timedelta(days=1),
        )
    )
    assert coord.zone_lockout_type("switch.old_seed") == ZONE_TURF
    assert coord.zone_lockout_type("switch.birdbath") == "birdbath"
    assert coord.is_zone_locked_out("switch.birdbath") is False
    coord._rain_lockout.until_by_type["birdbath"] = later
    assert coord.is_zone_locked_out("switch.birdbath") is True
    # Active rain watches the configured weather entity only.
    coord._config["weather_entity"] = "weather.home"
    states = {
        "weather.home": _state("rainy"),
        "weather.other": _state("pouring"),
    }
    coord._hass.states.get.side_effect = lambda eid: states.get(eid)
    assert coord._weather_states() == ["rainy"]


def test_disabled_establishment_schedule_does_not_exempt_the_zone():
    coord = _coord()
    later = dt_util.now() + timedelta(hours=5)
    coord._rain_lockout = StoredRainLockout(until_by_type={ZONE_TURF: later})
    today = dt_util.now().date()
    paused = _daily(
        "sprouts",
        "switch.sprouts",
        hour=6,
        minute=0,
        created_via="establishment",
        start_date=today - timedelta(days=1),
        end_date=today + timedelta(days=5),
        enabled=False,
    )
    coord.schedule_store.add(paused)
    assert coord.zone_lockout_type("switch.sprouts") == ZONE_TURF
    assert coord.is_zone_locked_out("switch.sprouts") is True
    coord.schedule_store.upsert(
        _daily(
            "sprouts",
            "switch.sprouts",
            hour=6,
            minute=0,
            created_via="establishment",
            start_date=today - timedelta(days=1),
            end_date=today + timedelta(days=5),
            enabled=True,
        )
    )
    assert coord.zone_lockout_type("switch.sprouts") == ZONE_GERMINATION
    assert coord.is_zone_locked_out("switch.sprouts") is False


def test_birdbath_clock_is_hidden_when_no_birdbath_zone_exists():
    coord = _coord()
    soon = dt_util.now() + timedelta(minutes=20)
    later = dt_util.now() + timedelta(hours=2)
    coord._rain_lockout = StoredRainLockout(until_by_type={"birdbath": soon, ZONE_TURF: later})
    coord._recompute_lockout_until()
    # The banner follows turf. The 20-minute birdbath hold does not.
    assert coord.lockout_until == later
    assert coord.is_locked_out_now() is True
    coord._rain_lockout = StoredRainLockout(until_by_type={"birdbath": soon})
    coord._recompute_lockout_until()
    assert coord.lockout_until is None
    assert coord.is_locked_out_now() is False
    assert coord.lockout_public_state()["by_type"]["birdbath"] is None
    coord.schedule_store.add(_daily("bath", "switch.birdbath", hour=6, minute=0))
    # The birdbath answer is cached for the tick. The next tick sees the zone.
    coord._lockout_tick_id += 1
    coord._recompute_lockout_until()
    assert coord.lockout_until == soon
    assert coord.is_locked_out_now() is True
    assert coord.lockout_public_state()["by_type"]["birdbath"] == soon.isoformat()


def test_birdbath_lookup_is_once_per_tick():
    coord = _coord()
    later = dt_util.now() + timedelta(hours=1)
    coord._rain_lockout = StoredRainLockout(until_by_type={ZONE_TURF: later, "birdbath": later})
    calls = {"n": 0}
    original = coord._classification_token

    def _counted():
        calls["n"] += 1
        return original()

    coord._classification_token = _counted
    coord._lockout_tick_id = 1
    assert coord._has_birdbath_zone() is False
    coord._recompute_lockout_until()
    assert coord.is_locked_out_now() is True
    assert calls["n"] == 1
    coord._lockout_tick_id = 2
    assert coord._has_birdbath_zone() is False
    assert calls["n"] == 2


def test_classification_cache_tracks_date_steps_and_friendly_name(monkeypatch):
    from custom_components.complete_irrigation.schedule import ZoneStep

    coord = _coord()
    today = dt_util.now().date()
    coord.schedule_store.add(
        _daily(
            "multi",
            "switch.a",
            hour=6,
            minute=0,
            zone_steps=(ZoneStep("switch.a", 10), ZoneStep("switch.b", 5)),
        )
    )
    token = coord._classification_token()
    assert "switch.b" in str(token)
    assert today in token
    states = {"switch.beds": _state("off", friendly_name="Front Beds")}
    coord._hass.states.get.side_effect = lambda eid: states.get(eid)
    coord._config["zones"] = {"switch.beds": {}}
    assert coord.zone_lockout_type("switch.beds") == ZONE_TURF
    states["switch.beds"] = _state("off", friendly_name="Flower Garden")
    assert coord.zone_lockout_type("switch.beds") == "flowers_garden"

    clock = {"now": dt_util.now()}

    def _now():
        return clock["now"]

    monkeypatch.setattr("homeassistant.util.dt.now", _now)
    fresh = _coord()
    fresh.schedule_store.add(
        _daily(
            "sprouts",
            "switch.sprouts",
            hour=6,
            minute=0,
            created_via="establishment",
            start_date=today - timedelta(days=3),
            end_date=today,
        )
    )
    assert fresh.zone_lockout_type("switch.sprouts") == ZONE_GERMINATION
    clock["now"] = clock["now"] + timedelta(days=2)
    assert fresh.zone_lockout_type("switch.sprouts") == ZONE_TURF


# ── extend service ────────────────────────────────────────────────


async def test_extend_service_adds_a_day_to_one_zone_type():
    coord = _coord()
    coord._notify_lockout_change = lambda: None
    until = dt_util.now() + timedelta(hours=3)
    coord._rain_lockout = StoredRainLockout(
        until_by_type={ZONE_TURF: until},
        released_zones={
            "switch.front_lawn": ZONE_TURF,
            "switch.trees": ZONE_DEEP_DRIP,
        },
    )
    hass = MagicMock()
    hass.data = {DOMAIN: {"entry": {"coordinator": coord}}}
    call = SimpleNamespace(
        data={"zone_type": "turf", "days": 1},
        context=SimpleNamespace(user_id=None),
    )
    await handle_extend_rain_lockout(hass, call)
    assert coord._rain_lockout.until_by_type[ZONE_TURF] - until == timedelta(days=1)
    # The other holding types were idle, so they were not started.
    assert coord._rain_lockout.until_by_type[ZONE_DEEP_DRIP] is None
    # A manual extend puts that type back under the clock.
    assert "switch.front_lawn" not in coord._rain_lockout.released_zones
    assert coord._rain_lockout.released_zones["switch.trees"] == ZONE_DEEP_DRIP


# ── sensor turns OFF at expiry ────────────────────────────────────


def test_sensor_turns_off_when_the_clock_expires(monkeypatch):
    coord = _coord()
    past = dt_util.now() - timedelta(minutes=1)
    coord._rain_lockout = StoredRainLockout(until_by_type={ZONE_TURF: past})
    coord._lockout_sensor_on = True  # stuck ON, the bug
    sensor = RainLockoutBinarySensor(coord, "entry")
    sensor.hass = MagicMock()
    sensor.async_write_ha_state = MagicMock()
    coord._sync_lockout_sensor()
    assert sensor.is_on is False
    sensor.async_write_ha_state.assert_called()

    # And the point-in-time callback does the same job at lockout_until.
    captured: dict = {}

    def _track(_hass, action, when):
        captured["action"] = action
        captured["when"] = when
        return lambda: None

    monkeypatch.setattr("homeassistant.helpers.event.async_track_point_in_time", _track)
    future = dt_util.now() + timedelta(hours=2)
    coord._rain_lockout = StoredRainLockout(until_by_type={ZONE_TURF: future})
    coord._lockout_sensor_on = True
    coord._schedule_lockout_expiry()
    assert captured["when"] == future
    coord._rain_lockout = StoredRainLockout(
        until_by_type={ZONE_TURF: dt_util.now() - timedelta(seconds=1)}
    )
    captured["action"](dt_util.now())
    assert sensor.is_on is False


# ── persistence round trip through the Store key ──────────────────


async def test_lockout_store_round_trip_does_not_touch_schedules(monkeypatch):
    disk: dict = {}
    saved: list[str] = []

    class FakeStore:
        def __init__(self, _hass, _version, key):
            self.key = key

        async def async_load(self):
            return disk.get(self.key)

        async def async_save(self, data):
            saved.append(self.key)
            disk[self.key] = copy.deepcopy(data)

    monkeypatch.setattr("homeassistant.helpers.storage.Store", FakeStore)
    until = dt_util.now().replace(microsecond=0) + timedelta(days=2)
    coord = _coord()
    coord._notify_lockout_change = lambda: None
    coord._rain_lockout = StoredRainLockout(
        until_by_type={ZONE_TURF: until, ZONE_DEEP_DRIP: until},
        last_event_rain_in=0.55,
        rain_source_disagree=True,
        forecast_extended={ZONE_TURF},
    )
    await coord.async_save_rain_lockout()
    assert saved == ["complete_irrigation.rain_lockout.test-entry"]
    assert not any("schedules" in key for key in saved)

    other = ScheduleCoordinator(MagicMock(), "test-entry")
    other._notify_lockout_change = lambda: None
    await other._load_rain_lockout()
    assert other._rain_lockout.until_by_type[ZONE_TURF] == until
    assert other._rain_lockout.until_by_type[ZONE_DEEP_DRIP] == until
    assert other._rain_lockout.last_event_rain_in == pytest.approx(0.55)
    assert other._rain_lockout.rain_source_disagree is True
    assert ZONE_TURF in other._rain_lockout.forecast_extended
    assert not any("schedules" in key for key in saved)


# ── schedules storage and enabled flags survive setup and reload ──


async def test_setup_and_reload_do_not_rewrite_schedules_or_enabled_flags(monkeypatch):
    disk: dict = {}
    saved: list[str] = []

    class FakeStore:
        def __init__(self, _hass, _version, key):
            self.key = key

        async def async_load(self):
            return copy.deepcopy(disk.get(self.key))

        async def async_save(self, data):
            saved.append(self.key)
            disk[self.key] = copy.deepcopy(data)

    held = Schedule(
        id="hold-1",
        name="Watering hold",
        zone_entity_id="switch.front_lawn",
        start_time=time(6, 0),
        duration_minutes=15,
        weekdays=(0, 1, 2, 3, 4, 5, 6),
        enabled=False,
    )
    live = Schedule(
        id="live-1",
        name="Still on",
        zone_entity_id="switch.trees",
        start_time=time(5, 30),
        duration_minutes=20,
        weekdays=(0, 2, 4),
        enabled=True,
    )
    entry = "hold-entry"
    schedules_key = f"complete_irrigation.schedules.{entry}"
    original = [held.to_dict(), live.to_dict()]
    disk[schedules_key] = {"schedules": copy.deepcopy(original)}
    disk[f"complete_irrigation.config.{entry}"] = {
        "zones": {},
        "first_run_at": "2026-01-01T00:00:00+00:00",
        "post_install_reminder_fired": True,
    }

    monkeypatch.setattr("homeassistant.helpers.storage.Store", FakeStore)
    monkeypatch.setattr(
        "homeassistant.helpers.event.async_track_time_interval",
        lambda *_a, **_k: lambda: None,
    )
    monkeypatch.setattr(
        "homeassistant.helpers.event.async_track_time_change",
        lambda *_a, **_k: lambda: None,
    )
    monkeypatch.setattr(
        "homeassistant.helpers.event.async_track_point_in_time",
        lambda *_a, **_k: lambda: None,
    )

    coord = ScheduleCoordinator(MagicMock(), entry)
    await coord.async_setup()
    assert coord.schedule_store.get("hold-1").enabled is False
    assert coord.schedule_store.get("live-1").enabled is True
    assert not any("schedules" in key for key in saved)

    coord.async_unload()
    await coord.async_setup()
    assert coord.schedule_store.get("hold-1").enabled is False
    assert coord.schedule_store.get("live-1").enabled is True
    assert disk[schedules_key]["schedules"] == original
    assert not any("schedules" in key for key in saved)


# ── zone type: turf fallback, override, diagnostic ────────────────


def test_unclassified_zone_is_turf_until_overridden():
    coord = _coord()
    coord._config["zones"] = {"switch.zone_9": {}}
    assert coord.zone_lockout_type("switch.zone_9") == ZONE_TURF
    report = coord.zone_lockout_report()["switch.zone_9"]
    assert report == {"type": ZONE_TURF, "override": None, "source": "classified"}
    public = coord.lockout_public_state()
    assert public["zones"]["switch.zone_9"]["type"] == ZONE_TURF

    coord._config["zones"]["switch.zone_9"]["lockout_zone_type"] = "deep_drip"
    assert coord.zone_lockout_type("switch.zone_9") == ZONE_DEEP_DRIP
    assert coord.zone_lockout_report()["switch.zone_9"]["source"] == "override"

    # An unknown override is ignored; classification (turf) stands.
    coord._config["zones"]["switch.zone_9"]["lockout_zone_type"] = "not-a-type"
    assert coord.zone_lockout_type("switch.zone_9") == ZONE_TURF
    assert coord.zone_lockout_report()["switch.zone_9"]["source"] == "classified"


# ── Open-Meteo: timeout, no per-tick fetch, cache never extends ──


def _payload(**overrides):
    base = {
        "past_72h_in": 0.30,
        "forecast_24h_in": 0.80,
        "forecast_24h_probability_pct": 90,
        "forecast_daily_eto_in": 0.12,
        "current_precip_mm": 0.0,
    }
    base.update(overrides)
    return base


async def test_open_meteo_request_sets_explicit_timeouts():
    import aiohttp

    from custom_components.complete_irrigation import coordinator as coord_mod

    coord = _coord()
    coord._hass.config.latitude = 33.3
    coord._hass.config.longitude = -111.8
    coord._hass.config.time_zone = "America/Phoenix"
    seen: dict = {}

    class _Resp:
        status = 500

    class _CM:
        async def __aenter__(self):
            return _Resp()

        async def __aexit__(self, *_a):
            return False

    class _Session:
        def get(self, url, timeout=None, **_k):
            seen["url"] = url
            seen["timeout"] = timeout
            return _CM()

    def _session(_hass):
        return _Session()

    # The fetch imports the helper inside the function.
    import homeassistant.helpers.aiohttp_client as aio_mod

    original = aio_mod.async_get_clientsession
    aio_mod.async_get_clientsession = _session
    try:
        result = await coord._fetch_open_meteo_rain(dt_util.now())
    finally:
        aio_mod.async_get_clientsession = original
    assert result is None
    timeout = seen["timeout"]
    assert isinstance(timeout, aiohttp.ClientTimeout)
    assert timeout.total == coord_mod._OM_TIMEOUT_TOTAL_S
    assert timeout.connect == coord_mod._OM_TIMEOUT_CONNECT_S
    assert timeout.sock_read == coord_mod._OM_TIMEOUT_SOCK_READ_S
    assert "past_days=3" in seen["url"]


async def test_open_meteo_body_is_read_to_eof_and_capped():
    import json
    from datetime import UTC, datetime

    coord = _coord()
    coord._hass.config.latitude = 33.3
    coord._hass.config.longitude = -111.8
    coord._hass.config.time_zone = "America/Phoenix"
    body = json.dumps(
        {
            "hourly": {
                "time": ["2026-11-04T10:00"],
                "precipitation": [25.4],
            }
        }
    ).encode()

    class _Content:
        def __init__(self, chunks):
            self._chunks = chunks

        async def iter_chunked(self, _size):
            for chunk in self._chunks:
                yield chunk

    class _Resp:
        def __init__(self, chunks, content_length=None, status=200):
            self.status = status
            self.content_length = content_length
            self.content = _Content(chunks)

    class _CM:
        def __init__(self, resp):
            self._resp = resp

        async def __aenter__(self):
            return self._resp

        async def __aexit__(self, *_a):
            return False

    class _Session:
        def __init__(self):
            self.resp = None

        def get(self, url, timeout=None, **_k):
            return _CM(self.resp)

    session = _Session()
    # Two chunks. read(n) would have kept only the first and failed JSON.
    session.resp = _Resp([body[:8], body[8:]])

    import homeassistant.helpers.aiohttp_client as aio_mod

    original = aio_mod.async_get_clientsession
    aio_mod.async_get_clientsession = lambda _hass: session
    try:
        moment = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
        parsed = await coord._fetch_open_meteo_rain(moment)
        assert parsed is not None
        assert parsed["past_72h_in"] == 1.0
        session.resp = _Resp([b"x" * 50], content_length=200_001)
        assert await coord._fetch_open_meteo_rain(dt_util.now()) is None
    finally:
        aio_mod.async_get_clientsession = original


async def test_cached_open_meteo_is_not_fetched_again_and_does_not_extend():
    coord = _coord()
    now = dt_util.now()
    coord._notify_lockout_change = lambda: None
    coord._read_heat_signal_f = lambda: 70.0
    coord._hass.states.async_entity_ids.return_value = []
    start = now + timedelta(hours=6)
    coord._rain_lockout = StoredRainLockout(
        until_by_type={ZONE_TURF: start},
        last_event_rain_in=0.30,
    )
    calls = {"n": 0}

    async def _fetch(_now):
        calls["n"] += 1
        return _payload()

    async def _history(*_a, **_k):
        return 0.30

    coord._fetch_open_meteo_rain = _fetch
    coord._tempest_history_inches = _history
    # The tick does not await the network. A finished refresh is what marks
    # the payload fresh for exactly one decision.
    await coord._refresh_open_meteo(now)
    assert calls["n"] == 1
    await coord._evaluate_lockout(now)
    assert coord._rain_lockout.until_by_type[ZONE_TURF] > start
    coord._rain_lockout.forecast_extended.clear()
    coord._rain_lockout.until_by_type[ZONE_TURF] = start
    await coord._evaluate_lockout(now + timedelta(minutes=1))
    assert calls["n"] == 1
    assert coord._rain_lockout.until_by_type[ZONE_TURF] == start


async def test_open_meteo_failure_uses_tempest_alone_without_disagree():
    coord = _coord()
    now = dt_util.now()
    coord._notify_lockout_change = lambda: None
    coord._read_heat_signal_f = lambda: 70.0
    coord._hass.states.async_entity_ids.return_value = []
    start = now + timedelta(hours=6)
    # A stale cached forecast that would extend, and a previous disagree flag.
    coord._open_meteo_rain = _payload(past_72h_in=3.0)
    coord._open_meteo_rain_at = now - timedelta(hours=2)
    coord._rain_lockout = StoredRainLockout(
        until_by_type={ZONE_TURF: start},
        last_event_rain_in=0.50,
        rain_source_disagree=True,
    )

    async def _fetch(_now):
        return None

    async def _history(*_a, **_k):
        return 0.50

    coord._fetch_open_meteo_rain = _fetch
    coord._tempest_history_inches = _history
    await coord._refresh_open_meteo(now)
    await coord._evaluate_lockout(now)
    assert coord._rain_lockout.rain_source_disagree is False
    assert coord._rain_lockout.until_by_type[ZONE_TURF] == start
    assert coord._open_meteo_rain is None
    # The failed fetch must not lower the baseline (that re-arms next time).
    assert coord._rain_lockout.last_event_rain_in == pytest.approx(0.50)


async def test_both_sources_down_uses_legacy_formula_and_does_not_stick():
    from custom_components.complete_irrigation.weather_gate import evaluate_rain_lockout

    coord = _coord(rain_sensor="sensor.rain")
    now = dt_util.now()
    coord._notify_lockout_change = lambda: None
    coord._read_heat_signal_f = lambda: 70.0  # 48h ceiling, does not bind 12h
    coord.eto_status = lambda: {"eto_in_week": 2.1}  # 0.30 in/day
    states = {"sensor.rain": _state("0.25", unit_of_measurement="in")}
    coord._hass.states.get.side_effect = lambda eid: states.get(eid)
    coord._hass.states.async_entity_ids.return_value = []

    async def _fetch(_now):
        return None

    async def _history(*_a, **_k):
        return None

    coord._fetch_open_meteo_rain = _fetch
    coord._tempest_history_inches = _history
    await coord._evaluate_lockout(now)
    expected = evaluate_rain_lockout(0.25, 2.1 / 7, max_hours=48)
    assert expected == 12
    for zone_type in (ZONE_TURF, ZONE_DEEP_DRIP, "flowers_garden"):
        until = coord._rain_lockout.until_by_type[zone_type]
        assert round((until - now).total_seconds() / 3600) == 12
    # The same cumulative total on the next tick must not push the clock.
    first = coord._rain_lockout.until_by_type[ZONE_TURF]
    await coord._evaluate_lockout(now + timedelta(minutes=1))
    assert coord._rain_lockout.until_by_type[ZONE_TURF] == first

    # No sensor either: an existing clock is left to expire, not extended.
    quiet = _coord()
    quiet._notify_lockout_change = lambda: None
    quiet._read_heat_signal_f = lambda: 70.0
    quiet._hass.states.async_entity_ids.return_value = []
    quiet._hass.states.get.return_value = None
    held = now + timedelta(hours=5)
    quiet._rain_lockout = StoredRainLockout(until_by_type={ZONE_TURF: held})
    quiet._fetch_open_meteo_rain = _fetch
    quiet._tempest_history_inches = _history
    await quiet._evaluate_lockout(now)
    assert quiet._rain_lockout.until_by_type[ZONE_TURF] == held


async def test_cached_blend_does_not_rearm_or_undo_clear():
    """OM 0.6 and Tempest 0.4 stay one blend across ticks, including after clear."""
    coord = _coord()
    now = dt_util.now()
    coord._notify_lockout_change = lambda: None
    coord._read_heat_signal_f = lambda: 70.0
    coord._hass.states.async_entity_ids.return_value = []
    calls = {"n": 0}

    async def _fetch(_now):
        calls["n"] += 1
        return _payload(past_72h_in=0.6, forecast_24h_in=0.9, forecast_24h_probability_pct=95)

    async def _history(*_a, **_k):
        return 0.4

    coord._fetch_open_meteo_rain = _fetch
    coord._tempest_history_inches = _history
    await coord._refresh_open_meteo(now)
    await coord._evaluate_lockout(now)
    assert calls["n"] == 1
    assert coord._rain_lockout.rain_source_disagree is False
    first = dict(coord._rain_lockout.until_by_type)
    baseline = coord._rain_lockout.last_event_rain_in
    await coord._evaluate_lockout(now + timedelta(minutes=1))
    await coord._evaluate_lockout(now + timedelta(minutes=2))
    assert calls["n"] == 1
    assert coord._rain_lockout.until_by_type == first
    assert coord._rain_lockout.last_event_rain_in == baseline
    assert coord._rain_lockout.rain_source_disagree is False
    coord.clear_lockout()
    await coord._evaluate_lockout(now + timedelta(minutes=3))
    assert calls["n"] == 1
    assert all(until is None for until in coord._rain_lockout.until_by_type.values())
    assert coord._rain_lockout.last_event_rain_in == baseline


async def test_expiry_callback_is_a_hass_callback():
    from homeassistant.core import HassJobType, get_hassjob_callable_job_type

    coord = _coord()
    coord._notify_lockout_change = lambda: None
    future = dt_util.now() + timedelta(hours=1)
    coord._rain_lockout = StoredRainLockout(until_by_type={ZONE_TURF: future})
    captured: dict = {}

    def _track(_hass, action, when):
        captured["action"] = action
        captured["when"] = when
        return lambda: None

    import homeassistant.helpers.event as event_mod

    original = event_mod.async_track_point_in_time
    event_mod.async_track_point_in_time = _track
    try:
        coord._schedule_lockout_expiry()
    finally:
        event_mod.async_track_point_in_time = original
    assert captured["when"] == future
    assert get_hassjob_callable_job_type(captured["action"]) is HassJobType.Callback


def test_expiry_recomputes_until_and_schedules_once():
    coord = _coord()
    coord._notify_lockout_change = lambda: None
    past = dt_util.now() - timedelta(minutes=1)
    future = dt_util.now() + timedelta(hours=2)
    coord._rain_lockout = StoredRainLockout(until_by_type={ZONE_TURF: past, ZONE_DEEP_DRIP: future})
    coord._lockout_sensor_on = True
    coord.lockout_until = past
    calls = {"n": 0}

    def _track(_hass, action, when):
        calls["n"] += 1
        calls["when"] = when
        return lambda: None

    import homeassistant.helpers.event as event_mod

    original = event_mod.async_track_point_in_time
    event_mod.async_track_point_in_time = _track
    try:
        coord._on_lockout_expiry(dt_util.now())
    finally:
        event_mod.async_track_point_in_time = original
    assert calls["n"] == 1
    assert calls["when"] == future
    assert coord.lockout_until == future


def _arm_fetch(coord):
    """Run Open-Meteo as a real background task, the way a tick does."""
    import asyncio

    tasks: list[asyncio.Task] = []

    def _create(coro):
        task = asyncio.get_running_loop().create_task(coro)
        tasks.append(task)
        return task

    coord._hass.async_create_task = _create
    coord._hass.config.latitude = 40.0
    coord._hass.config.longitude = -74.0
    return tasks


async def test_first_load_seeds_baseline_without_arming():
    import asyncio

    coord = _coord(rain_sensor="sensor.rain")
    now = dt_util.now()
    coord._notify_lockout_change = lambda: None
    coord._read_heat_signal_f = lambda: 70.0
    coord._hass.states.get.return_value = None
    coord._hass.states.async_entity_ids.return_value = []
    coord._rain_lockout = StoredRainLockout.from_dict({})
    assert coord._rain_lockout.baseline_seeded is False
    tasks = _arm_fetch(coord)

    async def _history(*_a, **_k):
        return 0.40

    async def _fetch(_now):
        await asyncio.sleep(0)
        return _payload(past_72h_in=0.60, forecast_24h_in=0.0, forecast_24h_probability_pct=0)

    coord._tempest_history_inches = _history
    coord._fetch_open_meteo_rain = _fetch
    await coord._evaluate_lockout(now)
    assert coord._rain_lockout.baseline_seeded is False
    assert tasks
    await tasks[0]
    await coord._evaluate_lockout(now + timedelta(minutes=1))
    # 0.40 and 0.60 agree, so the seed is their mean. Nothing arms.
    assert coord._rain_lockout.baseline_seeded is True
    assert coord._rain_lockout.last_event_rain_in == pytest.approx(0.50)
    assert coord._rain_lockout.baseline_by_source["tempest"] == pytest.approx(0.40)
    assert coord._rain_lockout.baseline_by_source["open_meteo"] == pytest.approx(0.60)
    assert all(until is None for until in coord._rain_lockout.until_by_type.values())
    await coord._evaluate_lockout(now + timedelta(minutes=2))
    assert all(until is None for until in coord._rain_lockout.until_by_type.values())


async def test_failed_open_meteo_seeds_tempest_and_later_success_does_not_arm():
    import asyncio

    coord = _coord(rain_sensor="sensor.rain")
    now = dt_util.now()
    coord._notify_lockout_change = lambda: None
    coord._read_heat_signal_f = lambda: 70.0
    coord._hass.states.get.return_value = None
    coord._hass.states.async_entity_ids.return_value = []
    coord._rain_lockout = StoredRainLockout.from_dict({})
    tasks = _arm_fetch(coord)

    async def _history(*_a, **_k):
        return 0.40

    async def _fetch(_now):
        await asyncio.sleep(0)
        return None

    coord._tempest_history_inches = _history
    coord._fetch_open_meteo_rain = _fetch
    await coord._evaluate_lockout(now)
    assert coord._rain_lockout.baseline_seeded is False
    await tasks[0]
    await coord._evaluate_lockout(now + timedelta(minutes=1))
    assert coord._rain_lockout.baseline_seeded is True
    assert coord._rain_lockout.last_event_rain_in == pytest.approx(0.40)
    assert all(until is None for until in coord._rain_lockout.until_by_type.values())
    coord._open_meteo_rain = _payload(
        past_72h_in=0.60, forecast_24h_in=0.0, forecast_24h_probability_pct=0
    )
    coord._om_unconsumed_fresh = True
    await coord._evaluate_lockout(now + timedelta(minutes=2))
    assert all(until is None for until in coord._rain_lockout.until_by_type.values())


async def test_open_meteo_refreshes_at_the_cache_boundary():
    from custom_components.complete_irrigation.coordinator import _OM_CACHE

    coord = _coord()
    now = dt_util.now()
    coord._open_meteo_rain = _payload(past_72h_in=0.40)
    coord._open_meteo_rain_at = now
    coord._om_unconsumed_fresh = False
    fetches = {"n": 0}

    async def _fetch(_now):
        fetches["n"] += 1
        return _payload(past_72h_in=0.40)

    coord._fetch_open_meteo_rain = _fetch
    tasks = _arm_fetch(coord)
    _payload_cached, kind = coord._open_meteo_for_decision(now + _OM_CACHE - timedelta(seconds=1))
    assert kind == "cached"
    assert fetches["n"] == 0
    _payload_due, kind = coord._open_meteo_for_decision(now + _OM_CACHE)
    assert kind == "unavailable"
    assert len(tasks) == 1
    await tasks[0]
    assert fetches["n"] == 1
    _payload_fresh, kind = coord._open_meteo_for_decision(now + _OM_CACHE)
    assert kind == "fresh"
    assert fetches["n"] == 1


async def test_tempest_dropout_and_recovery_does_not_rearm():
    coord = _coord(rain_sensor="sensor.rain")
    now = dt_util.now()
    coord._notify_lockout_change = lambda: None
    coord._read_heat_signal_f = lambda: 70.0
    coord._hass.states.get.return_value = None
    coord._hass.states.async_entity_ids.return_value = []
    coord._rain_lockout = StoredRainLockout(
        last_event_rain_in=0.45,
        baseline_by_source={"tempest": 0.30, "open_meteo": 0.60},
    )
    history = {"value": 0.30}

    async def _history(*_a, **_k):
        return history["value"]

    coord._tempest_history_inches = _history
    coord._open_meteo_rain = _payload(
        past_72h_in=0.60, forecast_24h_in=0.0, forecast_24h_probability_pct=0
    )
    coord._open_meteo_rain_at = now
    coord._om_unconsumed_fresh = False
    await coord._evaluate_lockout(now)
    assert all(until is None for until in coord._rain_lockout.until_by_type.values())
    history["value"] = None
    await coord._evaluate_lockout(now + timedelta(minutes=1))
    assert all(until is None for until in coord._rain_lockout.until_by_type.values())
    assert coord._rain_lockout.baseline_by_source["tempest"] == pytest.approx(0.30)
    history["value"] = 0.30
    await coord._evaluate_lockout(now + timedelta(minutes=2))
    assert all(until is None for until in coord._rain_lockout.until_by_type.values())
    history["value"] = 0.80
    await coord._evaluate_lockout(now + timedelta(minutes=3))
    assert coord._rain_lockout.until_by_type[ZONE_TURF] is not None


async def test_tempest_statistics_are_cached(monkeypatch):
    coord = _coord(rain_sensor="sensor.rain")
    now = dt_util.now()
    coord._hass.states.get.return_value = _state(
        "0.2", unit_of_measurement="in", state_class="total_increasing"
    )
    calls = {"n": 0}

    class _Instance:
        async def async_add_executor_job(self, fn, *args):
            calls["n"] += 1
            assert fn.__name__ == "statistics_during_period"
            return {
                "sensor.rain": [
                    {"start": (now - timedelta(hours=72)).timestamp(), "sum": 0.0, "state": 0.0},
                    {"start": (now - timedelta(hours=70)).timestamp(), "sum": 1.0, "state": 0.0},
                    {"start": (now - timedelta(hours=1)).timestamp(), "sum": 1.4, "state": 0.05},
                ]
            }

    monkeypatch.setattr(
        "homeassistant.components.recorder.get_instance",
        lambda _hass: _Instance(),
    )
    first = await coord._tempest_history_inches("sensor.rain", now)
    second = await coord._tempest_history_inches("sensor.rain", now + timedelta(minutes=1))
    assert calls["n"] == 1
    # 0.4 of sum growth, plus live 0.2 minus the same day's last state 0.05.
    # The exact -72h row is not the start of that sum.
    assert first == pytest.approx(0.55)
    assert second == first


async def test_cached_disagreement_does_not_flap():
    """A real gap stays flagged on cached ticks. 0.6 vs 0.4 agrees (see above)."""
    coord = _coord(rain_sensor="sensor.rain")
    now = dt_util.now()
    coord._notify_lockout_change = lambda: None
    coord._read_heat_signal_f = lambda: 70.0
    coord._hass.states.get.return_value = None
    coord._hass.states.async_entity_ids.return_value = []

    async def _history(*_a, **_k):
        return 0.20

    coord._tempest_history_inches = _history
    coord._open_meteo_rain = _payload(past_72h_in=0.60)
    coord._open_meteo_rain_at = now
    coord._om_unconsumed_fresh = True
    await coord._evaluate_lockout(now)
    assert coord._rain_lockout.rain_source_disagree is True
    flagged = coord._rain_lockout.last_event_rain_in
    await coord._evaluate_lockout(now + timedelta(minutes=1))
    await coord._evaluate_lockout(now + timedelta(minutes=2))
    assert coord._rain_lockout.rain_source_disagree is True
    assert coord._rain_lockout.last_event_rain_in == flagged


async def test_legacy_notification_names_only_types_that_armed():
    coord = _coord(rain_sensor="sensor.rain")
    now = dt_util.now()
    notes: list[dict] = []

    class _Notifier:
        async def notify(self, message, **kwargs):
            notes.append({"message": message, **kwargs})

    coord.notifier = _Notifier()
    coord._notify_lockout_change = lambda: None
    coord._read_heat_signal_f = lambda: 70.0
    coord.eto_status = lambda: {"eto_in_week": 2.1}
    states = {"sensor.rain": _state("0.25", unit_of_measurement="in")}
    coord._hass.states.get.side_effect = lambda eid: states.get(eid)
    coord._hass.states.async_entity_ids.return_value = []
    coord._rain_lockout = StoredRainLockout(last_event_rain_in=0.0, baseline_seeded=True)

    async def _none(*_a, **_k):
        return None

    coord._tempest_history_inches = _none
    coord._open_meteo_for_decision = lambda _now: (None, "unavailable")
    await coord._evaluate_lockout(now)
    assert len(notes) == 1
    assert notes[0]["title"] == "Rain lockout active"
    message = notes[0]["message"]
    assert message.startswith("Rain today (0.25 in) paused watering for ")
    named = message.removeprefix("Rain today (0.25 in) paused watering for ").removesuffix(".")
    assert set(part.strip() for part in named.split(",")) == {
        "deep_drip",
        "flowers_garden",
        "turf",
    }
    assert set(notes[0]["extra"]["zone_types"]) == {"deep_drip", "flowers_garden", "turf"}
    await coord._evaluate_lockout(now + timedelta(minutes=1))
    assert len(notes) == 1


async def test_lockout_paths_do_not_save_schedules_or_flip_enabled(monkeypatch):
    import custom_components.complete_irrigation.coordinator as coord_mod

    saved: list[str] = []

    async def _spy(*_a, **_k):
        saved.append("schedules")

    monkeypatch.setattr(coord_mod, "_async_save_schedules", _spy)
    coord = _coord()
    coord._notify_lockout_change = lambda: None
    coord.notifier = None
    held = _daily("hold", "switch.zone_9", hour=6, minute=0, enabled=False)
    live = _daily("live", "switch.zone_9", hour=7, minute=0, enabled=True)
    coord.schedule_store.add(held)
    coord.schedule_store.add(live)
    coord._rain_lockout = StoredRainLockout(
        until_by_type={ZONE_TURF: dt_util.now() + timedelta(hours=4)}
    )

    async def _noop(*_a, **_k):
        return None

    coord.async_save_config = _noop
    coord.async_save_rain_lockout = _noop
    coord.async_save_run_history = _noop
    coord._hass.async_create_task = lambda coro: coro.close()
    coord._hass.states.get.return_value = None
    coord._hass.states.async_entity_ids.return_value = []
    coord._last_tick = dt_util.now() - timedelta(seconds=30)

    await coord._tick(dt_util.now())
    await coord.note_zone_run_completed("switch.zone_9")
    coord.clear_lockout()
    assert saved == []
    assert coord.schedule_store.get("hold").enabled is False
    assert coord.schedule_store.get("live").enabled is True
