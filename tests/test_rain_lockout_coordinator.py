"""v1.40.3 — coordinator-level rain-lockout GLUE (the HA wiring around the pure
weather_gate math): unit-safe rainfall reading, the config floor, the F-normalized
heat signal (current temp + weather temp + forecast high), rising-edge arming, and
the forecast-high cache. The pure math is covered in test_weather_gate; THESE guard
the coordinator contract where the original 'mm read as inches' bug lived.

Needs HA importable (coordinator imports homeassistant.util.dt); skips on a bare box.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("homeassistant.util.dt")

from homeassistant.util import dt as dt_util

from custom_components.complete_irrigation.coordinator import ScheduleCoordinator


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


async def test_lockout_ceiling_uses_forecast_high_when_current_is_mild():
    """A hot cached forecast shortens the ceiling while the thermometer is mild."""

    def _build(*, forecast_f: float | None):
        coord = _coord(rain_sensor="sensor.rain", temperature_sensor="sensor.outdoor")
        coord._hass = _hass_with_states(
            {
                "sensor.rain": _state("2.0", unit_of_measurement="in"),
                "sensor.outdoor": _state("70", unit_of_measurement="°F"),
            },
            weather_ids=[],
        )
        coord.eto_status = lambda: {"eto_in_week": 2.1}
        coord._notify_lockout_change = lambda: None
        coord.notifier = None
        coord.lockout_until = None
        if forecast_f is not None:
            coord._forecast_high_f = forecast_f
            coord._forecast_high_at = dt_util.now()
        return coord

    now = dt_util.now()
    hot = _build(forecast_f=115.0)
    await hot._evaluate_lockout(now)
    assert hot.lockout_until is not None
    # 2.0 in wants a long lockout. 115F ceiling is 18h, not the 48h mild cap.
    assert round((hot.lockout_until - now).total_seconds() / 3600) == 18

    mild = _build(forecast_f=None)
    await mild._evaluate_lockout(now)
    assert mild.lockout_until is not None
    assert round((mild.lockout_until - now).total_seconds() / 3600) == 48


async def test_rain_fetch_refreshes_forecast_high_when_eto_auto_is_off():
    """70F now and a 115F forecast must ceiling at 18h with auto-ETo off.

    The HTTP body is the only stand-in. The cache is filled by the real
    fetch and parser, not by writing ``_forecast_high_f`` in the test.
    """
    import json

    import homeassistant.helpers.aiohttp_client as aio_mod

    now = dt_util.now()
    today = now.date()
    tomorrow_c = (115.0 - 32.0) * 5.0 / 9.0
    body = json.dumps(
        {
            "daily": {
                "time": [
                    (today - timedelta(days=3)).isoformat(),
                    (today - timedelta(days=2)).isoformat(),
                    (today - timedelta(days=1)).isoformat(),
                    today.isoformat(),
                    (today + timedelta(days=1)).isoformat(),
                ],
                # Past days are hotter than tomorrow. They must not set the ceiling.
                "temperature_2m_max": [50.0, 49.0, 48.0, 20.0, tomorrow_c],
            }
        }
    ).encode()
    seen: dict[str, str] = {}

    class _Content:
        async def iter_chunked(self, _size):
            yield body

    class _Resp:
        status = 200
        content_length = len(body)
        content = _Content()

    class _CM:
        async def __aenter__(self):
            return _Resp()

        async def __aexit__(self, *_args):
            return False

    class _Session:
        def get(self, url, timeout=None, **_kwargs):
            seen["url"] = url
            return _CM()

    coord = _coord(rain_sensor="sensor.rain", temperature_sensor="sensor.outdoor")
    assert not coord._config.get("eto_auto")
    coord._hass = _hass_with_states(
        {
            "sensor.rain": _state("2.0", unit_of_measurement="in"),
            "sensor.outdoor": _state("70", unit_of_measurement="°F"),
        },
        weather_ids=[],
    )
    coord._hass.config.latitude = 33.3
    coord._hass.config.longitude = -111.8
    coord._hass.config.time_zone = "America/Phoenix"
    coord.eto_status = lambda: {"eto_in_week": 2.1}
    coord._notify_lockout_change = lambda: None
    coord.notifier = None
    coord.lockout_until = None
    assert coord._forecast_high_f is None

    original = aio_mod.async_get_clientsession
    aio_mod.async_get_clientsession = lambda _hass: _Session()
    try:
        await coord._refresh_open_meteo(now)
    finally:
        aio_mod.async_get_clientsession = original

    assert "temperature_2m_max" in seen["url"]
    assert coord._forecast_high_f == pytest.approx(115.0, abs=0.05)
    assert coord._forecast_high_f < 120
    assert coord._forecast_high_at == now
    assert coord._forecast_high_is_fresh(now) is True

    await coord._evaluate_lockout(now)
    assert coord.lockout_until is not None
    assert round((coord.lockout_until - now).total_seconds() / 3600) == 18


async def test_missing_or_stale_forecast_is_on_the_sensor_and_warned_once(caplog):
    import logging

    from custom_components.complete_irrigation.binary_sensor import RainLockoutBinarySensor

    coord = _coord(temperature_sensor="sensor.outdoor", rain_sensor="sensor.rain")
    coord._hass = _hass_with_states(
        {
            "sensor.rain": _state("0.0", unit_of_measurement="in"),
            "sensor.outdoor": _state("70", unit_of_measurement="°F"),
        },
        weather_ids=[],
    )
    coord._notify_lockout_change = lambda: None
    coord.notifier = None
    now = dt_util.now()

    def _warnings():
        return [
            rec
            for rec in caplog.records
            if rec.levelno >= logging.WARNING and "forecast_fresh" in rec.message
        ]

    with caplog.at_level(logging.WARNING):
        await coord._evaluate_lockout(now)
        await coord._evaluate_lockout(now + timedelta(minutes=1))
        assert len(_warnings()) == 1
        await coord._evaluate_lockout(now + timedelta(hours=7))
        assert len(_warnings()) == 2

    public = coord.lockout_public_state()
    assert public["forecast_high_f"] is None
    assert public["forecast_fresh"] is False
    sensor = RainLockoutBinarySensor(coord, "entry")
    attrs = sensor.extra_state_attributes
    assert attrs["forecast_high_f"] is None
    assert attrs["forecast_fresh"] is False

    coord._forecast_high_f = 115.0
    coord._forecast_high_at = now - timedelta(hours=40)
    public = coord.lockout_public_state()
    assert public["forecast_high_f"] == pytest.approx(115.0)
    assert public["forecast_fresh"] is False
    attrs = sensor.extra_state_attributes
    assert attrs["forecast_high_f"] == pytest.approx(115.0)
    assert attrs["forecast_fresh"] is False
    # The stale high is visible, and it does not shorten the ceiling.
    assert round(coord._read_heat_signal_f()) == 70


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
    # 6.35mm = 0.25in, not 6.35in. No Tempest history and no Open-Meteo, so
    # the v1.62 formula applies: effective 0.15 / daily ETo 0.30 * 24 = 12h.
    coord = _lockout_coord("6.35", "mm")
    now = dt_util.now()
    await coord._evaluate_lockout(now)
    assert coord.lockout_until is not None
    assert round((coord.lockout_until - now).total_seconds() / 3600) == 12


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
