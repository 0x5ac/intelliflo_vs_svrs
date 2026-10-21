"""Config flow: the gateway's address and the SVRS restart setting.

The options flow changes the SVRS restart setting after setup.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback

from .const import (
    CONF_SVRS_RESTART,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_SVRS_RESTART,
    DOMAIN,
)


class CourtyardPumpConfigFlow(ConfigFlow, domain=DOMAIN):
    """One entry per gateway.

    There is no connection test: the gateway serves one client at a time,
    and the server loop reconnects with backoff anyway.
    """

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> PumpOptionsFlow:
        return PumpOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            await self.async_set_unique_id(
                f"{user_input[CONF_HOST]}:{user_input[CONF_PORT]}"
            )
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title="Courtyard fall", data=user_input)

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=DEFAULT_HOST): str,
                vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
                vol.Required(CONF_SVRS_RESTART, default=DEFAULT_SVRS_RESTART): bool,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)


class PumpOptionsFlow(OptionsFlowWithReload):
    """SVRS restart on or off. The entry reloads on save, and the new value
    reaches the pump the next time its registers are pinned (at once if it
    is stopped, otherwise when it next stops)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        entry = self.config_entry
        current = entry.options.get(
            CONF_SVRS_RESTART, entry.data.get(CONF_SVRS_RESTART, DEFAULT_SVRS_RESTART)
        )
        schema = vol.Schema({vol.Required(CONF_SVRS_RESTART, default=current): bool})
        return self.async_show_form(step_id="init", data_schema=schema)
