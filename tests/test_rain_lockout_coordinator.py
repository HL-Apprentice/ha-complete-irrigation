"""v1.40.3 — coordinator-level rain-lockout GLUE (the HA wiring around the pure
weather_gate math): unit-safe rainfall reading, the config floor, the F-normalized
heat signal (current temp + weather temp + forecast high), rising-edge arming, and
the forecast-high cache. The pure math is covered in test_weather_gate; THESE guard
the coordinator contract where the original 'mm read as inches' bug lived.

Needs HA importable (coordinator imports homeassistant.util.dt); skips on a bare box.
"""

from __future__ import annotations

from datetime import time, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("homeassistant.util.dt")

from homeassistant.util import dt as dt_util

from custom_components.complete_irrigation.const import DOMAIN
from custom_components.complete_irrigation.coordinator import (
    ScheduleCoordinator,
    _async_load_lockout,
    _async_save_lockout,
)
from custom_components.complete_irrigation.rain_lockout import (
    RAIN_SOURCE_DISAGREE,
    TYPE_DEEP_DRIP,
    TYPE_FLOWERS_GARDEN,
    TYPE_TURF,
)
from custom_components.complete_irrigation.schedule import Schedule
from custom_components.complete_irrigation.services import handle_extend_rain_lockout


def _coord(**config) -> ScheduleCoordinator:
    coord = ScheduleCoordinator(MagicMock(), "test-entry")
    coord._config.update(config)
    return coord


def _state(value, **attrs) -> MagicMock:
    st = MagicMock()
    st.state = value
    st.attributes = dict(attrs)
    return st


def _hass_with_states(states_map, weather_ids=()) -> MagicMock:
    hass = MagicMock()
    hass.states.get.side_effect = lambda eid: states_map.get(eid)
    hass.states.async_entity_ids.side_effect = lambda domain: (
        list(weather_ids) if domain == "weather" else []
    )
    return hass


# ── _read_heat_signal_f: F-normalized MAX of the three sources ───────


def test_heat_signal_converts_celsius_and_takes_max():
    coord = _coord(temperature_sensor="sensor.outdoor")
    coord._hass = _hass_with_states(
        {
            "sensor.outdoor": _state("40", unit_of_measurement="°C"),  # 104F
            "weather.home": _state("sunny", temperature=30, temperature_unit="°C"),  # 86F
        },
        weather_ids=["weather.home"],
    )
    assert round(coord._read_heat_signal_f()) == 104  # hottest wins, C->F applied


def test_heat_signal_includes_fresh_forecast_high():
    coord = _coord(temperature_sensor="sensor.outdoor")
    coord._hass = _hass_with_states(
        {"sensor.outdoor": _state("90", unit_of_measurement="°F")}, weather_ids=[]
    )
    # cool current temp, but a HOT forecast high -> the forecast wins.
    coord._forecast_high_f = 112.0
    coord._forecast_high_at = dt_util.now()
    assert coord._read_heat_signal_f() == 112.0


def test_heat_signal_ignores_stale_forecast_high():
    coord = _coord(temperature_sensor="sensor.outdoor")
    coord._hass = _hass_with_states(
        {"sensor.outdoor": _state("90", unit_of_measurement="°F")}, weather_ids=[]
    )
    coord._forecast_high_f = 112.0
    coord._forecast_high_at = dt_util.now() - timedelta(hours=40)  # >36h stale
    assert coord._read_heat_signal_f() == 90  # forecast dropped, only current temp


def test_heat_signal_none_when_no_sources():
    coord = _coord()
    coord._hass = _hass_with_states({}, weather_ids=[])
    assert coord._read_heat_signal_f() is None


# ── _evaluate_lockout: unit-safe reading + floor + rising edge ───────


def _lockout_coord(rain_value, unit, *, floor=None, eto_week=2.1, heat_f=105.0):
    cfg = {"rain_sensor": "sensor.rain"}
    if floor is not None:
        cfg["rain_lockout_min_inches"] = floor
    coord = _coord(**cfg)
    coord._hass = MagicMock()
    coord._hass.states.get.return_value = _state(rain_value, unit_of_measurement=unit)
    coord.eto_status = lambda: {"eto_in_week": eto_week}
    coord._read_heat_signal_f = lambda: heat_f
    coord._notify_lockout_change = lambda: None
    coord.notifier = None
    coord.lockout_until = None
    return coord


async def test_lockout_reads_mm_not_as_inches():
    # 6.35mm = 0.25in exactly. Must not be read as 6.35 inches.
    # 0.25in is the 2/1/2-day band. Weekly ETo 2.1 → daily 0.30, scale clamps
    # to 0.5x, and 105F's ceiling is 24h. Flowers stay at 12h, deep drip at 24h.
    from custom_components.complete_irrigation.rain_lockout import (
        TYPE_DEEP_DRIP,
        TYPE_FLOWERS_GARDEN,
        TYPE_TURF,
        scaled_lockout_hours,
    )

    coord = _lockout_coord("6.35", "mm")
    now = dt_util.now()
    await coord._evaluate_lockout(now)
    assert coord._last_combined_in == pytest.approx(0.25, abs=1e-6)
    daily = 2.1 / 7.0
    for zone_type in (TYPE_DEEP_DRIP, TYPE_FLOWERS_GARDEN, TYPE_TURF):
        hours = scaled_lockout_hours(0.25, zone_type, daily, 105.0)
        assert coord._lockouts[zone_type] == now + timedelta(hours=hours)
    assert scaled_lockout_hours(0.25, TYPE_FLOWERS_GARDEN, daily, 105.0) == 12
    assert scaled_lockout_hours(0.25, TYPE_DEEP_DRIP, daily, 105.0) == 24
    assert coord.lockout_until == now + timedelta(hours=24)


async def test_lockout_trace_mm_does_not_lock():
    # 0.25mm = 0.01in, below the 0.10in floor -> no lockout (the reported bug).
    coord = _lockout_coord("0.25", "mm")
    await coord._evaluate_lockout(dt_util.now())
    assert coord.lockout_until is None


async def test_lockout_respects_config_floor():
    # 0.4in real rain, but floor raised to 0.5 -> no lockout.
    coord = _lockout_coord("0.4", "in", floor=0.5)
    await coord._evaluate_lockout(dt_util.now())
    assert coord.lockout_until is None


async def test_lockout_only_arms_on_rising_edge():
    coord = _lockout_coord("0.5", "in")
    now = dt_util.now()
    await coord._evaluate_lockout(now)
    first = coord.lockout_until
    assert first is not None
    # the same cumulative reading on the next tick must NOT re-extend the lockout.
    await coord._evaluate_lockout(now + timedelta(minutes=1))
    assert coord.lockout_until == first


def _weekday(zone, **overrides) -> Schedule:
    fields = {
        "id": zone,
        "name": zone,
        "zone_entity_id": zone,
        "start_time": time(6, 0),
        "duration_minutes": 10,
        "weekdays": (0, 1, 2, 3, 4, 5, 6),
    }
    fields.update(overrides)
    return Schedule(**fields)


def test_zone_types_birdbath_germination_and_ignore_flag():
    now = dt_util.now()
    coord = _coord(
        zones={
            "switch.lawn": {"category": "lawn"},
            "switch.trees": {"category": "trees"},
            "switch.garden": {"category": "vegetable_garden"},
        },
        independent_zones=["switch.bath"],
    )
    coord._hass = _hass_with_states({})
    coord._lockouts = {
        TYPE_TURF: now + timedelta(days=2),
        TYPE_DEEP_DRIP: now + timedelta(days=4),
        TYPE_FLOWERS_GARDEN: now + timedelta(days=1),
    }
    assert coord.zone_is_locked("switch.lawn", now)
    assert coord.zone_is_locked("switch.trees", now)
    assert coord.zone_is_locked("switch.garden", now)
    # Birdbath: no multi-day hold, only while rain is falling.
    assert coord.zone_is_locked("switch.bath", now) is False
    coord._active_rain = True
    assert coord.zone_is_locked("switch.bath", now) is True
    coord._active_rain = False
    germ = _weekday(
        "switch.lawn",
        id="germ",
        name="New planting lawn cycle 1",
        created_via="establishment",
    )
    coord.schedule_store.add(germ)
    assert coord.zone_is_locked("switch.lawn", now, germ) is False
    # The zone itself is germinating while that schedule is active, so a
    # second schedule on it is not held either.
    assert coord.zone_is_locked("switch.lawn", now) is False
    opted_out = _weekday("switch.trees", id="trees", ignore_rain_lockout=True)
    assert opted_out.ignore_rain_lockout is True
    assert coord.schedule_is_locked(opted_out, now) is False
    assert coord.zone_is_locked("switch.trees", now) is True


def test_completed_run_releases_only_that_zone():
    now = dt_util.now()
    coord = _coord(
        zones={
            "switch.front": {"category": "lawn"},
            "switch.back": {"category": "lawn"},
        }
    )
    coord._hass = _hass_with_states({})
    until = now + timedelta(days=2)
    coord._lockouts[TYPE_TURF] = until
    coord.note_zone_run_completed("switch.front")
    assert coord.zone_is_locked("switch.front", now) is False
    assert coord.zone_is_locked("switch.back", now) is True
    # The type deadline did not move.
    assert coord._lockouts[TYPE_TURF] == until


def test_moisture_releases_early_and_never_extends():
    now = dt_util.now()
    coord = _coord(
        zones={
            "switch.lawn": {
                "category": "lawn",
                "moisture_entities": ["sensor.lawn_m"],
                "target_pct": 31,
            }
        }
    )
    until = now + timedelta(days=2)
    coord._lockouts[TYPE_TURF] = until
    coord._zone_moisture_reading = lambda cfg: (10.0, ["sensor.lawn_m"], [10.0], [])
    assert coord._apply_moisture_releases() is False
    assert "switch.lawn" not in coord._released_zones
    assert coord._lockouts[TYPE_TURF] == until
    coord._zone_moisture_reading = lambda cfg: (45.0, ["sensor.lawn_m"], [45.0], [])
    assert coord._apply_moisture_releases() is True
    assert "switch.lawn" in coord._released_zones
    assert coord._lockouts[TYPE_TURF] == until
    assert coord.zone_is_locked("switch.lawn", now) is False


def test_forecast_extends_an_active_hold_by_one_day_once():
    now = dt_util.now()
    coord = _coord()
    coord._read_heat_signal_f = lambda: 70.0  # ceiling 48h, so +24h still fits
    coord._lockouts[TYPE_TURF] = now + timedelta(hours=10)
    assert coord._maybe_extend_for_forecast(now, {"forecast_extends": True}) is True
    assert coord._lockouts[TYPE_TURF] == now + timedelta(hours=10, days=1)
    again = coord._lockouts[TYPE_TURF]
    assert coord._maybe_extend_for_forecast(now, {"forecast_extends": True}) is False
    assert coord._lockouts[TYPE_TURF] == again
    # Does not start a hold that rain itself did not start.
    coord._lockouts.clear()
    coord._forecast_extended_on.clear()
    assert coord._maybe_extend_for_forecast(now, {"forecast_extends": True}) is False
    assert TYPE_TURF not in coord._lockouts


def test_forecast_extension_stays_inside_the_heat_ceiling():
    now = dt_util.now()
    coord = _coord()
    coord._read_heat_signal_f = lambda: 105.0  # ceiling 24h from now
    coord._lockouts[TYPE_DEEP_DRIP] = now + timedelta(hours=20)
    coord._maybe_extend_for_forecast(now, {"forecast_extends": True})
    # +1 day would be 44h out; the ceiling clips it to 24h from now.
    assert coord._lockouts[TYPE_DEEP_DRIP] == now + timedelta(hours=24)


async def test_disagreement_flag_and_single_source_fallback():
    now = dt_util.now()
    coord = _coord()
    coord._hass = _hass_with_states({})
    coord._read_heat_signal_f = lambda: 80.0
    coord.eto_status = lambda: {"eto_in_week": 0.84}  # daily 0.12, scale 1
    coord._read_tempest_72h = AsyncMock(return_value=(0.20, now))
    coord._read_open_meteo_rain = AsyncMock(
        return_value={"precip_72h_in": 1.20, "last_rain_at": now, "forecast_extends": False}
    )
    await coord._evaluate_lockout(now)
    assert coord._rain_source_disagree is True
    assert coord.lockout_attributes()[RAIN_SOURCE_DISAGREE] is True
    assert coord._last_combined_in == pytest.approx(0.45)
    assert coord._rain_source == "conservative"

    coord._read_tempest_72h = AsyncMock(return_value=None)
    coord._event_rain_in = None
    coord._read_open_meteo_rain = AsyncMock(
        return_value={"precip_72h_in": 0.40, "last_rain_at": now, "forecast_extends": False}
    )
    await coord._evaluate_lockout(now)
    assert coord._rain_source_disagree is False
    assert coord._rain_source == "open_meteo"
    assert coord._last_combined_in == pytest.approx(0.40)


async def test_lockout_persistence_round_trip(monkeypatch):
    stored: dict[str, dict] = {}

    class _Store:
        def __init__(self, hass, version, key):
            self.key = key

        async def async_load(self):
            return stored.get(self.key)

        async def async_save(self, data):
            stored[self.key] = data

    monkeypatch.setattr("homeassistant.helpers.storage.Store", _Store)
    now = dt_util.now()
    coord = _coord()
    coord._lockouts = {
        TYPE_TURF: now + timedelta(days=2),
        TYPE_DEEP_DRIP: now + timedelta(days=4),
    }
    coord._released_zones = {"switch.front"}
    coord._event_rain_in = 0.8
    coord._event_anchor = now - timedelta(hours=6)
    coord._forecast_extended_on = {TYPE_TURF: now.date().isoformat()}
    coord._rain_source_disagree = True
    coord._last_combined_in = 0.8
    coord._rain_source = "mean"
    await _async_save_lockout(coord._hass, coord._entry_id, coord._lockout_state())
    assert stored
    assert all("schedules" not in key for key in stored)

    loaded = await _async_load_lockout(coord._hass, coord._entry_id)
    fresh = ScheduleCoordinator(MagicMock(), coord._entry_id)
    fresh._restore_lockout_state(loaded)
    assert fresh._lockouts[TYPE_TURF] == coord._lockouts[TYPE_TURF]
    assert fresh._lockouts[TYPE_DEEP_DRIP] == coord._lockouts[TYPE_DEEP_DRIP]
    assert fresh._released_zones == {"switch.front"}
    assert fresh._event_rain_in == pytest.approx(0.8)
    assert fresh._event_anchor == coord._event_anchor
    assert fresh._forecast_extended_on[TYPE_TURF] == now.date().isoformat()
    assert fresh._rain_source_disagree is True
    assert TYPE_FLOWERS_GARDEN not in fresh._lockouts


async def test_extend_service_adds_hours_to_one_type():
    coord = _coord()
    coord._hass = MagicMock()
    before = dt_util.now()
    hass = MagicMock()
    hass.data = {DOMAIN: {"entry": {"coordinator": coord}}}
    call = MagicMock()
    call.data = {"hours": 6, "zone_type": "turf"}
    await handle_extend_rain_lockout(hass, call)
    assert coord._lockouts[TYPE_TURF] >= before + timedelta(hours=6) - timedelta(seconds=2)
    assert TYPE_DEEP_DRIP not in coord._lockouts
    assert TYPE_FLOWERS_GARDEN not in coord._lockouts
    # A second call extends the same hold rather than restarting it.
    first = coord._lockouts[TYPE_TURF]
    call.data = {"hours": 2, "zone_type": "turf"}
    await handle_extend_rain_lockout(hass, call)
    assert coord._lockouts[TYPE_TURF] >= first + timedelta(hours=2) - timedelta(seconds=2)


async def test_gate_records_skip_for_a_locked_run():
    now = dt_util.now().replace(hour=6, minute=0, second=30, microsecond=0)
    last = now - timedelta(minutes=1)
    coord = _coord(zones={"switch.lawn": {"category": "lawn"}})
    coord._hass = _hass_with_states({})
    coord.notifier = None
    coord.schedule_store.add(_weekday("switch.lawn", name="Morning lawn"))
    coord._lockouts[TYPE_TURF] = now + timedelta(days=1)
    coord._last_combined_in = 0.5
    recorded = {}
    original = coord._record_lockout_skips

    async def _spy(schedules, since, moment, zone_buffer):
        recorded["called"] = True
        return await original(schedules, since, moment, zone_buffer)

    coord._record_lockout_skips = _spy
    coord._evaluate_lockout = AsyncMock()
    coord._hass.async_create_task = lambda coro: coro.close()
    coord._last_tick = last
    await coord._tick(now)
    assert recorded.get("called") is True
    skipped = [rec for rec in coord.run_history.all() if rec.status == "skipped"]
    assert len(skipped) == 1
    assert skipped[0].reason == "rain lockout"
    assert skipped[0].zone_entity_id == "switch.lawn"
    assert skipped[0].triggers["rain_lockout"]["zone_type"] == TYPE_TURF


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_sensor_turns_off_at_expiry():
    from custom_components.complete_irrigation.binary_sensor import RainLockoutBinarySensor

    coord = _coord()
    coord._hass = MagicMock()
    future = dt_util.now() + timedelta(hours=2)
    coord._lockouts[TYPE_TURF] = future
    coord._lockout_published_on = True
    sensor = RainLockoutBinarySensor(coord, "entry")
    sensor.hass = MagicMock()
    sensor.async_write_ha_state = MagicMock()
    assert sensor.is_on is True
    # Expiry without a publish leaves the entity unwritten — the stuck-on bug.
    coord._lockouts[TYPE_TURF] = dt_util.now() - timedelta(seconds=5)
    sensor.async_write_ha_state.assert_not_called()
    assert sensor.is_on is False
    coord._publish_lockout()
    sensor.async_write_ha_state.assert_called()
    assert sensor.is_on is False


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_expiry_timer_publishes_off(monkeypatch):
    from custom_components.complete_irrigation.binary_sensor import RainLockoutBinarySensor

    coord = _coord()
    captured = {}

    def _track(_hass, action, moment):
        captured["action"] = action
        captured["moment"] = moment
        return lambda: None

    monkeypatch.setattr("homeassistant.helpers.event.async_track_point_in_time", _track)
    deadline = dt_util.now() + timedelta(hours=1)
    coord._lockouts[TYPE_TURF] = deadline
    coord._arm_lockout_expiry()
    assert captured["moment"] == deadline
    sensor = RainLockoutBinarySensor(coord, "entry")
    sensor.hass = MagicMock()
    sensor.async_write_ha_state = MagicMock()
    coord._lockout_published_on = True
    coord._lockouts[TYPE_TURF] = dt_util.now() - timedelta(seconds=1)
    captured["action"](dt_util.now())
    sensor.async_write_ha_state.assert_called()
    assert sensor.is_on is False


# ── forecast-high cache wired into _refresh_auto_eto ─────────────────


async def test_refresh_caches_forecast_high_in_f():
    coord = _coord(eto_in_week=2.0, eto_auto=True, weather_entity="weather.home")
    hass = MagicMock()
    hass.config.latitude = 33.4
    hass.config.elevation = 337
    state = MagicMock()
    state.attributes = {"temperature_unit": "°C", "wind_speed_unit": "km/h"}
    hass.states.get.return_value = state
    hass.states.async_entity_ids.return_value = ["weather.home"]
    fc = [
        {"temperature": 44.0, "templow": 25.0},
        {"temperature": 40.0, "templow": 22.0},
    ]
    hass.services.async_call = AsyncMock(return_value={"weather.home": {"forecast": fc}})
    coord._hass = hass
    await coord._refresh_auto_eto()
    # max(44, 40) = 44C -> 111.2F, cached with a timestamp.
    assert coord._forecast_high_f == pytest.approx(111.2, abs=0.1)
    assert coord._forecast_high_at is not None
