"""Regression tests for the two watering-core fixes confirmed by the v1.33.2
multi-model loop review (both in coordinator.py):

1. Auto-soak's re-cycle branch was missing the same-zone "already on" guard that
   the cycle-1 branch has — a soak cycle-2 could fire on top of a live scheduled/
   manual run for the SAME zone and truncate it (under-water).
2. The hot-weather runtime boost had no upper clamp — a long base run x a high
   boost could exceed MAX_SCHEDULE_DURATION_MIN and, dispatched fire-and-forget,
   be dropped silently by the service boundary on the hottest days.

These drive the real ScheduleCoordinator methods (the coordinator imports
homeassistant.util.dt, hence the importorskip), not a re-implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("homeassistant.util.dt")

from custom_components.complete_irrigation.const import MAX_SCHEDULE_DURATION_MIN
from custom_components.complete_irrigation.coordinator import ScheduleCoordinator

NOW = datetime(2026, 6, 16, 6, 20, 0)
ZONE = "switch.lawn"


@dataclass
class _FakeState:
    state: str
    attributes: dict = field(default_factory=dict)


def _soaking_coord(switch_state: str) -> ScheduleCoordinator:
    """A coordinator parked mid-soak (cycle 1, wait expired) for ZONE, whose
    moisture is still below min, with ZONE's switch reporting `switch_state`."""
    coord = ScheduleCoordinator(MagicMock(), "test-entry")
    coord._config.update(
        zones={ZONE: {"auto_soak_enabled": True, "min_pct": 30, "soak_max_cycles": 4}},
        active_run_sessions={},
    )
    coord.is_locked_out_now = lambda: False
    coord._soak_state = {
        ZONE: {"phase": "soaking", "until": NOW - timedelta(minutes=1), "cycle": 1}
    }
    # Effective moisture 10% is below the 30% min → the sequence wants another cycle.
    coord._zone_moisture_reading = lambda cfg: (10.0, ["sensor.lawn_m"], [], [])
    coord._start_soak_run = AsyncMock()
    coord._hass = MagicMock()
    coord._hass.states.get.return_value = _FakeState(switch_state)
    return coord


async def test_recycle_skips_when_same_zone_switch_is_on():
    """The fix: a live run for THIS zone (switch 'on') must block soak cycle-2 so
    it can't truncate the scheduled/manual run."""
    coord = _soaking_coord("on")
    await coord._tick_auto_soak(NOW)
    coord._start_soak_run.assert_not_called()
    # The sequence is left intact to re-cycle once the zone is genuinely idle.
    assert ZONE in coord._soak_state


async def test_recycle_fires_when_zone_switch_is_off():
    """Control: with the zone idle (switch 'off'), the re-cycle still fires — the
    guard is narrow and must not suppress a legitimate next cycle."""
    coord = _soaking_coord("off")
    await coord._tick_auto_soak(NOW)
    coord._start_soak_run.assert_awaited_once()
    _args, kwargs = coord._start_soak_run.await_args
    assert kwargs["cycle"] == 2


def test_hot_weather_boost_clamps_to_max():
    """A 300-min base x a 100% boost (x2.0) = 600 must clamp to the 480 ceiling,
    not be dispatched over-limit (which the fire-and-forget path would drop)."""
    coord = ScheduleCoordinator(MagicMock(), "test-entry")
    coord._config.update(hot_threshold_f=100, boost_percent=100)
    coord._read_forecast_high = lambda: 110.0
    new_minutes, note = coord._evaluate_hot_weather_gate(300, {})
    assert new_minutes == MAX_SCHEDULE_DURATION_MIN == 480
    assert note is not None  # a boost was applied and noted


def test_hot_weather_boost_unclamped_below_ceiling():
    """A boost that stays under the ceiling passes through unchanged."""
    coord = ScheduleCoordinator(MagicMock(), "test-entry")
    coord._config.update(hot_threshold_f=100, boost_percent=50)
    coord._read_forecast_high = lambda: 110.0
    new_minutes, _note = coord._evaluate_hot_weather_gate(100, {})
    assert new_minutes == 150  # 100 x 1.5, no clamp
