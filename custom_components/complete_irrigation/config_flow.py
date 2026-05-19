"""Config flow for HA Complete Irrigation Integration.

Minimal stub for the v0.1 scaffold. The full multi-step wizard (controller
auto-detection, zone selection, sensor area/label, plant categories, weather
binding, conflict policy, notifications) is built across later vertical
slices documented in docs/ISSUES.md.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN, NAME


class CompleteIrrigationConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Complete Irrigation."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Initial setup step.

        v0.1 just creates the config entry so the integration loads. Future
        slices replace this with the full wizard.
        """
        # Only one instance of this integration is allowed during scaffold.
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            return self.async_create_entry(title=NAME, data={})

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({}),
        )
