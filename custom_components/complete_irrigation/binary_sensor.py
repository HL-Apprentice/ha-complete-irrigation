"""Binary sensor platform — rain lockout status.

`binary_sensor.complete_irrigation_rain_lockout` is `on` while a
rain-driven lockout is active (no schedules will fire), with the lockout
end time exposed as the `until` attribute.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN, NAME

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator = entry_data["coordinator"]
    async_add_entities([RainLockoutBinarySensor(coordinator, entry.entry_id)])


class RainLockoutBinarySensor(BinarySensorEntity):
    """True while the integration is in a rain-driven lockout."""

    _attr_has_entity_name = True
    _attr_name = "Rain lockout"
    _attr_icon = "mdi:weather-pouring"
    _attr_should_poll = False  # coordinator triggers updates

    def __init__(self, coordinator, entry_id: str) -> None:
        self._coordinator = coordinator
        self._entry_id = entry_id
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_rain_lockout"
        coordinator.register_lockout_listener(self._lockout_changed)

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry_id)},
            name=NAME,
            manufacturer="HL-Apprentice",
            entry_type=None,
        )

    @property
    def is_on(self) -> bool:
        return self._coordinator.is_locked_out_now()

    @property
    def extra_state_attributes(self) -> dict:
        until: datetime | None = self._coordinator.lockout_until
        return {"until": until.isoformat() if until else None}

    def _lockout_changed(self) -> None:
        """Called by the coordinator on state changes."""
        if self.hass is not None:
            self.async_write_ha_state()
