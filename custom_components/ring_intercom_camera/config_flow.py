"""Config flow for Ring Intercom Video Camera.

No credentials are needed: the integration reuses the auth from the official
Ring integration, so the flow is just a single confirmation step. Only one
instance makes sense (it discovers every intercom from every Ring entry).
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN

RING_DOMAIN = "ring"


class RingIntercomCameraConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Ring Intercom Video Camera."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if not self.hass.config_entries.async_entries(RING_DOMAIN):
            return self.async_abort(reason="ring_not_configured")

        if user_input is not None:
            return self.async_create_entry(
                title="Ring Intercom Video Camera", data={}
            )

        return self.async_show_form(step_id="user")

    async def async_step_import(
        self, import_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Import a config entry from configuration.yaml."""
        return await self.async_step_user(user_input={})
