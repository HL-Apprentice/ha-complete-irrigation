"""v1.28 — coordinator-level auto-ETo wiring (effective_eto + _refresh_auto_eto).

The pure source-selection policy is covered in test_et_calc (select_eto). THESE
guard the actual coordinator contract the watering directive depends on, which the
adversarial gate flagged as untested:

  * effective_eto() ALWAYS returns a positive number and never raises;
  * the 36h staleness gate falls back to manual;
  * an out-of-range auto value is rejected (never the design basis);
  * _refresh_auto_eto degrades gracefully — no-op when off; leaves the prior value
    intact when the forecast errors / is empty / yields garbage; stores a sane
    rounded value + timestamp on success.

Needs HA importable (the coordinator's methods import homeassistant.util.dt), so
these run in CI and skip on a bare dev machine like the other HA-tier tests.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("homeassistant.util.dt")

from homeassistant.util import dt as dt_util

from custom_components.complete_irrigation.coordinator import (
    ScheduleCoordinator,
)


def _coord(**config) -> ScheduleCoordinator:
    coord = ScheduleCoordinator(MagicMock(), "test-entry")
    coord._config.update(config)
    return coord


def _async_hass(
    *,
    forecast=None,
    raise_on_call=False,
    weather_present=True,
    temp_unit="°C",
    wind_unit="km/h",
    lat=33.4,
    elev=337,
) -> MagicMock:
    """A hass whose weather.get_forecasts is awaitable (AsyncMock)."""
    hass = MagicMock()
    hass.config.latitude = lat
    hass.config.elevation = elev
    state = MagicMock()
    state.attributes = {"temperature_unit": temp_unit, "wind_speed_unit": wind_unit}
    hass.states.get.return_value = state if weather_present else None
    hass.states.async_entity_ids.return_value = ["weather.home"] if weather_present else []
    if raise_on_call:
        hass.services.async_call = AsyncMock(side_effect=RuntimeError("boom"))
    else:
        hass.services.async_call = AsyncMock(
            return_value={"weather.home": {"forecast": forecast or []}}
        )
    return hass


# ── effective_eto: staleness / fallback / always-positive ────────────


def test_effective_eto_manual_when_auto_off():
    coord = _coord(eto_in_week=2.0, eto_auto=False)
    coord._auto_eto_in_week = 3.3
    coord._auto_eto_at = dt_util.now()
    assert coord.effective_eto() == 2.0
    assert coord.eto_status()["eto_source"] == "manual"


def test_effective_eto_auto_when_fresh():
    coord = _coord(eto_in_week=2.0, eto_auto=True)
    coord._auto_eto_in_week = 3.3
    coord._auto_eto_at = dt_util.now()
    assert coord.effective_eto() == 3.3
    status = coord.eto_status()
    assert status["eto_source"] == "auto"
    assert status["eto_manual"] == 2.0
    assert status["eto_auto_value"] == 3.3


def test_effective_eto_falls_back_when_stale():
    coord = _coord(eto_in_week=2.0, eto_auto=True)
    coord._auto_eto_in_week = 3.3
    coord._auto_eto_at = dt_util.now() - timedelta(hours=40)  # > 36h window
    assert coord.effective_eto() == 2.0
    assert coord.eto_status()["eto_source"] == "manual"


def test_effective_eto_falls_back_when_never_computed():
    coord = _coord(eto_in_week=2.0, eto_auto=True)
    assert coord._auto_eto_in_week is None
    assert coord.effective_eto() == 2.0
    assert coord.eto_status()["eto_source"] == "manual"


def test_effective_eto_rejects_out_of_range_auto():
    # A fresh-but-absurd value must NOT become the design basis (clamp -> manual).
    coord = _coord(eto_in_week=2.0, eto_auto=True)
    coord._auto_eto_in_week = 50.0
    coord._auto_eto_at = dt_util.now()
    assert coord.effective_eto() == 2.0
    assert coord.eto_status()["eto_source"] == "manual"


def test_effective_eto_always_positive_on_garbage_config():
    coord = _coord(eto_in_week="bad", eto_auto=True)
    coord._auto_eto_in_week = -5  # insane
    coord._auto_eto_at = dt_util.now()
    val = coord.effective_eto()
    assert val > 0  # default 1.5 — never the negative auto value, never raises


# ── _refresh_auto_eto: graceful degradation ──────────────────────────


async def test_refresh_noop_when_auto_off():
    coord = _coord(eto_in_week=2.0, eto_auto=False)
    coord._hass = _async_hass(forecast=[{"temperature": 42, "templow": 25}])
    await coord._refresh_auto_eto()
    assert coord._auto_eto_in_week is None
    coord._hass.services.async_call.assert_not_called()


async def test_refresh_success_stores_sane_value():
    coord = _coord(eto_in_week=2.0, eto_auto=True, weather_entity="weather.home")
    fc = [{"temperature": 42.0, "templow": 25.0, "humidity": 25, "wind_speed": 7.2}] * 5
    coord._hass = _async_hass(forecast=fc)
    await coord._refresh_auto_eto()
    assert coord._auto_eto_in_week is not None
    assert 0 < coord._auto_eto_in_week <= 10
    assert coord._auto_eto_at is not None


async def test_refresh_keeps_prior_on_empty_forecast():
    coord = _coord(eto_in_week=2.0, eto_auto=True, weather_entity="weather.home")
    coord._auto_eto_in_week = 2.5
    coord._auto_eto_at = dt_util.now()
    coord._hass = _async_hass(forecast=[])
    await coord._refresh_auto_eto()
    assert coord._auto_eto_in_week == 2.5  # unchanged


async def test_refresh_keeps_prior_on_service_error():
    coord = _coord(eto_in_week=2.0, eto_auto=True, weather_entity="weather.home")
    coord._auto_eto_in_week = 2.5
    coord._auto_eto_at = dt_util.now()
    coord._hass = _async_hass(raise_on_call=True)
    await coord._refresh_auto_eto()  # must not raise
    assert coord._auto_eto_in_week == 2.5  # unchanged


async def test_refresh_noop_when_no_weather_entity():
    coord = _coord(eto_in_week=2.0, eto_auto=True)
    coord._hass = _async_hass(weather_present=False)
    await coord._refresh_auto_eto()
    assert coord._auto_eto_in_week is None


async def test_refresh_rejects_out_of_range_value(monkeypatch):
    # Force the computation to yield an absurd figure -> must NOT be stored.
    import custom_components.complete_irrigation.coordinator as cmod

    coord = _coord(eto_in_week=2.0, eto_auto=True, weather_entity="weather.home")
    coord._hass = _async_hass(forecast=[{"temperature": 42, "templow": 25}])
    monkeypatch.setattr(cmod, "weekly_eto_inches_from_forecast", lambda *a, **k: 50.0)
    await coord._refresh_auto_eto()
    assert coord._auto_eto_in_week is None  # rejected, fell back


async def test_refresh_keeps_prior_when_computation_raises(monkeypatch):
    import custom_components.complete_irrigation.coordinator as cmod

    def _boom(*a, **k):
        raise ValueError("bad forecast row")

    coord = _coord(eto_in_week=2.0, eto_auto=True, weather_entity="weather.home")
    coord._auto_eto_in_week = 2.5
    coord._auto_eto_at = dt_util.now()
    coord._hass = _async_hass(forecast=[{"temperature": 42, "templow": 25}])
    monkeypatch.setattr(cmod, "weekly_eto_inches_from_forecast", _boom)
    await coord._refresh_auto_eto()  # must not raise
    assert coord._auto_eto_in_week == 2.5  # unchanged
