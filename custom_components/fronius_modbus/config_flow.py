from __future__ import annotations

from homeassistant import config_entries
from homeassistant.core import callback

from .const import DOMAIN
from .flow_common import (
    TokenFlowMixin,
    async_delete_token,
    async_update_entry_from_input,
    default_payload,
    entry_defaults,
    entry_payload,
)


class ConfigFlow(TokenFlowMixin, config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = 1
    MINOR_VERSION = 3
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    def __init__(self) -> None:
        self._pending_flow_state = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return FroniusModbusOptionsFlow(config_entry)

    async def _async_finish_user(
        self,
        settings,
        info,
        previous_host,
    ):
        del previous_host
        return self.async_create_entry(
            title=info["title"],
            data=entry_payload(settings, reconfigure_required=False),
        )

    async def _async_finish_reconfigure(
        self,
        settings,
        info,
        previous_host,
    ):
        del info
        entry = self._get_reconfigure_entry()
        await async_update_entry_from_input(
            self.hass,
            entry,
            settings,
            previous_host=previous_host,
        )
        return self.async_abort(reason="reconfigure_successful")

    async def async_step_user(self, user_input=None):
        return await self._async_handle_settings_step(
            user_input=user_input,
            step_id="user",
            password_step_id="user_password",
            defaults=default_payload(),
            previous_host=None,
            on_success=self._async_finish_user,
        )

    async def async_step_user_password(self, user_input=None):
        return await self._async_handle_password_step(
            user_input=user_input,
            step_id="user_password",
            restart_step=self.async_step_user,
            on_success=self._async_finish_user,
        )

    async def async_step_reconfigure(self, user_input=None):
        entry = self._get_reconfigure_entry()
        defaults = entry_defaults(entry)
        return await self._async_handle_settings_step(
            user_input=user_input,
            step_id="reconfigure",
            password_step_id="reconfigure_password",
            defaults=defaults,
            previous_host=defaults["host"],
            on_success=self._async_finish_reconfigure,
        )

    async def async_step_reconfigure_password(self, user_input=None):
        return await self._async_handle_password_step(
            user_input=user_input,
            step_id="reconfigure_password",
            restart_step=self.async_step_reconfigure,
            on_success=self._async_finish_reconfigure,
        )


class FroniusModbusOptionsFlow(TokenFlowMixin, config_entries.OptionsFlow):
    """Handle Fronius Modbus options."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry
        self._pending_flow_state = None

    async def _async_finish_options(
        self,
        settings,
        info,
        previous_host,
    ):
        del info
        if previous_host != settings["host"]:
            await async_delete_token(self.hass, previous_host)
        return self.async_create_entry(
            title="",
            data=entry_payload(settings, reconfigure_required=False),
        )

    async def async_step_init(self, user_input=None):
        defaults = entry_defaults(self.config_entry)
        return await self._async_handle_settings_step(
            user_input=user_input,
            step_id="init",
            password_step_id="password",
            defaults=defaults,
            previous_host=defaults["host"],
            on_success=self._async_finish_options,
        )

    async def async_step_password(self, user_input=None):
        return await self._async_handle_password_step(
            user_input=user_input,
            step_id="password",
            restart_step=self.async_step_init,
            on_success=self._async_finish_options,
        )
