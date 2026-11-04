"""Coordinator wiring for the v1.64 rain lockout.

Needs Home Assistant importable (Store, dt, the binary sensor). Skips on a
bare box, same as the other coordinator tests. The policy math itself is in
test_rain_lockout_policy.py and does not need HA.
"""

from __future__ import annotations

import copy
from datetime import time, timedelta
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
    coord.schedule_store.add(_daily("sprouts", "switch.germination", hour=6, minute=0))
    # Every type looks locked. Germination is still never gated. Birdbath is,
    # because an active-rain hold sets its clock. The ignore flag opts out.
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
    assert zones == ["switch.birdbath", "switch.front_lawn"]
    assert all(rec.reason == "rain lockout" for rec in skipped)
    assert all(rec.triggers["rain_lockout"]["zone_type"] for rec in skipped)
    # The ignored lawn schedule and the germination zone produced no row.
    assert len(skipped) == 2


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
    await coord.note_zone_run_completed("switch.front_lawn")
    assert coord._rain_lockout.until_by_type[ZONE_TURF] is None
    assert coord._rain_lockout.until_by_type[ZONE_DEEP_DRIP] == deep
    # The baseline stays, so the same 72h total cannot immediately re-lock turf.
    assert coord._rain_lockout.last_event_rain_in == pytest.approx(0.4)


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

        async def _no_meteo(*_args, **_kwargs):
            return None, "failed"

        coord._tempest_history_inches = _no_history
        coord._read_open_meteo_rain = _no_meteo
        coord._rain_lockout = StoredRainLockout(
            until_by_type={ZONE_TURF: turf_until, ZONE_DEEP_DRIP: deep_until},
            last_event_rain_in=0.40,
        )
        return coord

    dry = _run("10")
    await dry._evaluate_lockout(now)
    assert dry._rain_lockout.until_by_type[ZONE_TURF] is None
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

    async def _forecast(_now):
        return (
            {
                "past_72h_in": 0.30,
                "forecast_24h_in": 0.40,
                "forecast_24h_probability_pct": 70,
                "forecast_daily_eto_in": 0.12,
                "current_precip_mm": 0.0,
            },
            "fresh",
        )

    coord._tempest_history_inches = _no_history
    coord._read_open_meteo_rain = _forecast
    await coord._evaluate_lockout(now)
    assert coord._rain_lockout.until_by_type[ZONE_TURF] == now + timedelta(hours=36)
    await coord._evaluate_lockout(now + timedelta(minutes=1))
    assert coord._rain_lockout.until_by_type[ZONE_TURF] == now + timedelta(hours=36)


def test_germination_is_never_locked_and_birdbath_only_with_a_clock():
    coord = _coord()
    later = dt_util.now() + timedelta(hours=5)
    coord._rain_lockout = StoredRainLockout(
        until_by_type={ZONE_GERMINATION: later, "birdbath": None, ZONE_TURF: later}
    )
    assert coord.zone_lockout_type("switch.germination_bed") == ZONE_GERMINATION
    assert coord.is_zone_locked_out("switch.germination_bed") is False
    assert coord.zone_lockout_type("switch.birdbath") == "birdbath"
    assert coord.is_zone_locked_out("switch.birdbath") is False
    coord._rain_lockout.until_by_type["birdbath"] = later
    assert coord.is_zone_locked_out("switch.birdbath") is True


# ── extend service ────────────────────────────────────────────────


async def test_extend_service_adds_a_day_to_one_zone_type():
    coord = _coord()
    coord._notify_lockout_change = lambda: None
    until = dt_util.now() + timedelta(hours=3)
    coord._rain_lockout = StoredRainLockout(until_by_type={ZONE_TURF: until})
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
    await coord._evaluate_lockout(now)
    assert calls["n"] == 1
    assert coord._rain_lockout.until_by_type[ZONE_TURF] > start
    # Room to extend again, and the once-per-episode flag cleared. The cache
    # must still refuse both a second fetch and another extension.
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
    await coord._evaluate_lockout(now)
    assert coord._rain_lockout.rain_source_disagree is False
    assert coord._rain_lockout.until_by_type[ZONE_TURF] == start
    assert coord._open_meteo_rain is None


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
