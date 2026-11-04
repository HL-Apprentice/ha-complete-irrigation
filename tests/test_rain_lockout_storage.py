"""Schedules storage is not rewritten when rain lockout loads.

A live watering hold is schedules with enabled=False. Coordinator setup and a
second setup (reload) may write the new rain-lockout file and the existing
config file. They must not write ``complete_irrigation.schedules.*`` or flip
enabled / ignore_rain_lockout.

Uses Home Assistant's own core (the full CI job). Skips where HA is not
installed. The pytest-homeassistant-custom-component plugin is intentionally
not required: its autouse cleanup fixtures fail the rest of this suite.
"""

from __future__ import annotations

import tempfile
from datetime import time

import pytest

pytest.importorskip("homeassistant")

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from custom_components.complete_irrigation.coordinator import ScheduleCoordinator
from custom_components.complete_irrigation.schedule import Schedule


def _hold(schedule_id: str, *, enabled: bool, ignore_rain: bool) -> dict:
    sched = Schedule(
        id=schedule_id,
        name=f"Hold {schedule_id}",
        zone_entity_id="switch.front_lawn",
        start_time=time(5, 30),
        duration_minutes=20,
        weekdays=(0, 1, 2, 3, 4, 5, 6),
        enabled=enabled,
        ignore_rain_lockout=ignore_rain,
    )
    return sched.to_dict()


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
async def test_setup_and_reload_do_not_touch_schedules_storage(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        hass = HomeAssistant(tmp)
        entry_id = "hold-entry"
        key = f"complete_irrigation.schedules.{entry_id}"
        original = {
            "schedules": [
                _hold("paused", enabled=False, ignore_rain=False),
                _hold("bird", enabled=True, ignore_rain=True),
            ]
        }
        await Store(hass, 1, key).async_save(original)

        saved_keys: list[str] = []
        original_save = Store.async_save

        async def _spy(self, data):
            saved_keys.append(self.key)
            return await original_save(self, data)

        monkeypatch.setattr(Store, "async_save", _spy)

        try:
            coord = ScheduleCoordinator(hass, entry_id)
            await coord.async_setup()
            paused = coord.schedule_store.get("paused")
            bird = coord.schedule_store.get("bird")
            assert paused is not None and paused.enabled is False
            assert paused.ignore_rain_lockout is False
            assert bird is not None and bird.enabled is True
            assert bird.ignore_rain_lockout is True
            coord.async_unload()

            reloaded = ScheduleCoordinator(hass, entry_id)
            await reloaded.async_setup()
            assert reloaded.schedule_store.get("paused").enabled is False
            assert reloaded.schedule_store.get("paused").ignore_rain_lockout is False
            assert reloaded.schedule_store.get("bird").enabled is True
            assert reloaded.schedule_store.get("bird").ignore_rain_lockout is True
            reloaded.async_unload()
            loaded = await Store(hass, 1, key).async_load()
        finally:
            await hass.async_stop()

    assert key not in saved_keys
    assert loaded == original
    assert loaded["schedules"][0]["enabled"] is False
    assert loaded["schedules"][0]["ignore_rain_lockout"] is False
    assert loaded["schedules"][1]["enabled"] is True
    assert loaded["schedules"][1]["ignore_rain_lockout"] is True
