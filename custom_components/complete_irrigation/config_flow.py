"""Config flow for HA Complete Irrigation Integration.

Multi-step wizard:
  1. user   — auto-detect installed irrigation integrations and ask which
              one (or "Manual — pick switches myself") to import zones from
  2. zones  — show candidate zone switches (filtered through the
              `select_zone_candidates` helper) and let the user confirm

Sensor setup, plant categories, weather binding, conflict policy, and
notifications all come in later slices (#7, #6, #5, #10).
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from .category_match import suggest_category as _suggest_category
from .const import DOMAIN, NAME
from .helpers import detect_irrigation_integrations, select_zone_candidates

MANUAL_MODE = "__manual__"


class CompleteIrrigationConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Multi-step setup for Complete Irrigation."""

    VERSION = 1

    def __init__(self) -> None:
        # `None` means manual switch-picker mode (no specific source integration).
        self._controller_domain: str | None = None
        # Zones the user picked in step 2; held in flow state until the
        # final create_entry call after the optional aliases step.
        self._selected_zones: list[str] = []
        # entity_id → current friendly name (computed before step 3) so the
        # aliases form has sensible defaults.
        self._zone_defaults: dict[str, str] = {}

    # ── Step 1: pick irrigation integration ───────────────────────────
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Detect installed irrigation integrations and let user pick."""
        # v0.1 limits to one instance — multi-controller support is later work.
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        installed_domains = set(self.hass.config_entries.async_domains())
        detected = detect_irrigation_integrations(installed_domains)

        if user_input is not None:
            choice = user_input["controller"]
            self._controller_domain = None if choice == MANUAL_MODE else choice
            return await self.async_step_zones()

        # Build the selector options
        options: list[selector.SelectOptionDict] = [
            selector.SelectOptionDict(value=domain, label=label) for domain, label in detected
        ]
        options.append(
            selector.SelectOptionDict(
                value=MANUAL_MODE,
                label="None of these — let me pick switches manually",
            )
        )

        default = detected[0][0] if detected else MANUAL_MODE

        schema = vol.Schema(
            {
                vol.Required("controller", default=default): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options,
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    # ── Step 2: pick zones ────────────────────────────────────────────
    async def async_step_zones(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Show candidate zone switches and let the user confirm."""
        # Build entity descriptors from BOTH the entity registry (config-
        # entry-based integrations like Rachio) AND hass.states (YAML
        # entities like template / command_line / MQTT YAML / RPi GPIO).
        registry = er.async_get(self.hass)
        config_entries_by_id = {
            ce.entry_id: ce.domain for ce in self.hass.config_entries.async_entries()
        }
        entities: list[dict[str, Any]] = []
        registry_entity_ids: set[str] = set()
        for e in registry.entities.values():
            registry_entity_ids.add(e.entity_id)
            entities.append(
                {
                    "entity_id": e.entity_id,
                    "domain": e.domain,
                    "config_entry_domain": (
                        config_entries_by_id.get(e.config_entry_id) if e.config_entry_id else None
                    ),
                    "name": e.name,
                    "original_name": e.original_name,
                    "disabled": e.disabled,
                }
            )
        # Append non-registry switches (template, command_line, MQTT YAML, etc.)
        for state in self.hass.states.async_all("switch"):
            if state.entity_id in registry_entity_ids:
                continue
            entities.append(
                {
                    "entity_id": state.entity_id,
                    "domain": "switch",
                    "config_entry_domain": None,
                    "name": state.attributes.get("friendly_name"),
                    "original_name": None,
                    "disabled": False,
                }
            )

        candidates = select_zone_candidates(entities, controller_domain=self._controller_domain)

        if user_input is not None:
            # Stash selections for the optional aliases step
            self._selected_zones = user_input.get("zones", [])
            # Pre-compute current friendly names so the aliases form has
            # something readable to show even for entities without a name.
            self._zone_defaults = {}
            for c in candidates:
                if c["entity_id"] in self._selected_zones:
                    fallback = (
                        c.get("name")
                        or c.get("original_name")
                        or c["entity_id"].split(".", 1)[-1].replace("_", " ").title()
                    )
                    self._zone_defaults[c["entity_id"]] = fallback
            return await self.async_step_aliases()

        if not candidates:
            return self.async_abort(reason="no_zones_found")

        zone_options = [
            selector.SelectOptionDict(
                value=c["entity_id"],
                label=f"{c.get('name') or c.get('original_name') or c['entity_id']} "
                f"({c['entity_id']})",
            )
            for c in candidates
        ]

        schema = vol.Schema(
            {
                vol.Required(
                    "zones",
                    default=[c["entity_id"] for c in candidates],
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=zone_options,
                        mode=selector.SelectSelectorMode.LIST,
                        multiple=True,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="zones", data_schema=schema)

    # ── Step 3: optional rename + name-based category defaults ──────
    async def async_step_aliases(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Let the user rename zones (PRD #13) before the integration commits.

        Also computes a suggested plant category from the (renamed) zone
        name (PRD #20) so the Sensors tab starts with sensible defaults.
        """
        if user_input is not None:
            # Build {entity_id: alias} only for entries the user actually
            # changed away from the default.
            aliases = {}
            categories = {}
            for entity_id in self._selected_zones:
                key = f"alias_{entity_id}"
                alias = (user_input.get(key) or "").strip()
                if alias and alias != self._zone_defaults.get(entity_id):
                    aliases[entity_id] = alias
                # Auto-suggest plant category from name
                source_name = alias or self._zone_defaults.get(entity_id, "")
                cat = _suggest_category(source_name)
                if cat:
                    categories[entity_id] = cat
            return self.async_create_entry(
                title=NAME,
                data={
                    "controller_domain": self._controller_domain,
                    "zones": self._selected_zones,
                    "zone_aliases": aliases,
                    "zone_category_suggestions": categories,
                },
            )

        # Build the form: one text field per zone, defaulted to current name.
        schema_dict: dict[Any, Any] = {}
        for entity_id in self._selected_zones:
            default = self._zone_defaults.get(entity_id, entity_id)
            schema_dict[vol.Optional(f"alias_{entity_id}", default=default)] = (
                selector.TextSelector()
            )
        return self.async_show_form(
            step_id="aliases",
            data_schema=vol.Schema(schema_dict),
            description_placeholders={"zone_count": str(len(self._selected_zones))},
        )
