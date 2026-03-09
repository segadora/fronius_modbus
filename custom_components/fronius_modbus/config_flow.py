from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries, exceptions
from homeassistant.core import HomeAssistant, callback

from .hub import Hub
from homeassistant.const import CONF_NAME, CONF_HOST, CONF_PORT, CONF_SCAN_INTERVAL
from .const import (
    DOMAIN,
    DEFAULT_NAME,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_INVERTER_UNIT_ID,
    DEFAULT_METER_UNIT_ID,
    DEFAULT_AUTO_ENABLE_MODBUS,
    CONF_INVERTER_UNIT_ID,
    CONF_METER_UNIT_ID,
    CONF_API_USERNAME,
    CONF_API_PASSWORD,
    CONF_AUTO_ENABLE_MODBUS,
    CONF_RECONFIGURE_REQUIRED,
    FIXED_API_USERNAME,
    SUPPORTED_MANUFACTURERS,
    SUPPORTED_MODELS,
)

_LOGGER = logging.getLogger(__name__)


def _default_payload() -> dict[str, Any]:
    return {
        CONF_NAME: DEFAULT_NAME,
        CONF_HOST: "",
        CONF_PORT: DEFAULT_PORT,
        CONF_INVERTER_UNIT_ID: DEFAULT_INVERTER_UNIT_ID,
        CONF_METER_UNIT_ID: DEFAULT_METER_UNIT_ID,
        CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
        CONF_API_USERNAME: FIXED_API_USERNAME,
        CONF_API_PASSWORD: "",
        CONF_AUTO_ENABLE_MODBUS: DEFAULT_AUTO_ENABLE_MODBUS,
    }


def _expand_user_input(user_input: dict[str, Any], defaults: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = _default_payload()
    if defaults:
        payload.update(defaults)
    payload[CONF_HOST] = str(user_input.get(CONF_HOST, payload[CONF_HOST])).strip()
    payload[CONF_API_PASSWORD] = str(user_input.get(CONF_API_PASSWORD, payload[CONF_API_PASSWORD]))
    payload[CONF_API_USERNAME] = FIXED_API_USERNAME
    return payload


def _entry_payload(data: dict[str, Any], *, reconfigure_required: bool) -> dict[str, Any]:
    payload = dict(data)
    payload[CONF_RECONFIGURE_REQUIRED] = reconfigure_required
    return payload

def _build_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
            vol.Required(
                CONF_API_PASSWORD,
                default=defaults.get(CONF_API_PASSWORD, ""),
            ): str,
        }
    )

async def validate_input(hass: HomeAssistant, data: dict) -> dict[str, Any]:
    """Validate the user input allows us to connect.

    Data has the keys from DATA_SCHEMA with values provided by the user.
    """
    if len(data[CONF_HOST]) < 3:
        raise InvalidHost
    if data[CONF_PORT] > 65535:
        raise InvalidPort
    if data[CONF_SCAN_INTERVAL] < 5:
        raise ScanIntervalTooShort

    if data[CONF_METER_UNIT_ID] > 0:
        meter_addresses = [data[CONF_METER_UNIT_ID]]
    else:
        meter_addresses = []

    api_password = data.get(CONF_API_PASSWORD, "")
    if api_password == "":
        raise MissingApiPassword
    api_username = FIXED_API_USERNAME

    all_addresses = meter_addresses + [data[CONF_INVERTER_UNIT_ID]]

    if len(all_addresses) > len(set(all_addresses)):
        _LOGGER.error(f"Modbus addresses are not unique {all_addresses}")
        raise AddressesNotUnique

    hub = Hub(
        hass,
        data[CONF_NAME],
        data[CONF_HOST],
        data[CONF_PORT],
        data[CONF_INVERTER_UNIT_ID],
        meter_addresses,
        data[CONF_SCAN_INTERVAL],
        api_username=api_username or None,
        api_password=api_password or None,
        auto_enable_modbus=data.get(CONF_AUTO_ENABLE_MODBUS, DEFAULT_AUTO_ENABLE_MODBUS),
    )
    try:
        if api_username and not await hub.validate_web_api():
            raise InvalidApiCredentials
        await hub.init_data(setup_coordinator=False)
    except Exception as e:
        _LOGGER.error(f"Cannot start hub {e}")
        if isinstance(e, InvalidApiCredentials):
            raise
        raise CannotConnect
    finally:
        hub.close()

    manufacturer = hub.data.get('i_manufacturer')
    if manufacturer is None:
        _LOGGER.error(f"No manufacturer is returned")
        raise UnsupportedHardware   
    if manufacturer not in SUPPORTED_MANUFACTURERS:
        _LOGGER.error(f"Unsupported manufacturer: '{manufacturer}'")
        raise UnsupportedHardware

    model = hub.data.get('i_model')
    if model is None:
        _LOGGER.error(f"No model type is returned")
        raise UnsupportedHardware

    supported = False
    for supported_model in SUPPORTED_MODELS:
        if model.startswith(supported_model):
            supported = True
    
    if not supported:
        _LOGGER.warning(f"Untested model {model}")

    return {"title": data[CONF_NAME]}

class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow """

    VERSION = 1
    MINOR_VERSION = 2
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return FroniusModbusOptionsFlow(config_entry)

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}
        defaults = _default_payload()
        if user_input is not None:
            try:
                validated_input = _expand_user_input(user_input, defaults)
                info = await validate_input(self.hass, validated_input)

                return self.async_create_entry(
                    title=info["title"],
                    data=_entry_payload(validated_input, reconfigure_required=False),
                )
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidPort:
                errors["base"] = "invalid_port"
            except InvalidHost:
                errors["host"] = "invalid_host"
            except ScanIntervalTooShort:
                errors["base"] = "scan_interval_too_short"
            except MissingApiPassword:
                errors["base"] = "missing_api_password"
            except InvalidApiCredentials:
                errors["base"] = "invalid_api_credentials"
            except UnsupportedHardware:
                errors["base"] = "unsupported_hardware"
            except AddressesNotUnique:
                errors["base"] = "modbus_address_conflict"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user",
            data_schema=_build_schema(defaults),
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input=None):
        """Handle reconfiguration of an existing entry."""
        errors = {}
        entry = self._get_reconfigure_entry()
        defaults = _expand_user_input({}, {**entry.data, **entry.options})

        if user_input is not None:
            try:
                validated_input = _expand_user_input(user_input, defaults)
                await validate_input(self.hass, validated_input)
                updated_payload = _entry_payload(validated_input, reconfigure_required=False)
                self.hass.config_entries.async_update_entry(
                    entry,
                    data={**entry.data, **updated_payload},
                    options={**entry.options, **updated_payload},
                    title=validated_input[CONF_NAME],
                )
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reconfigure_successful")
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidPort:
                errors["base"] = "invalid_port"
            except InvalidHost:
                errors["host"] = "invalid_host"
            except ScanIntervalTooShort:
                errors["base"] = "scan_interval_too_short"
            except MissingApiPassword:
                errors["base"] = "missing_api_password"
            except InvalidApiCredentials:
                errors["base"] = "invalid_api_credentials"
            except UnsupportedHardware:
                errors["base"] = "unsupported_hardware"
            except AddressesNotUnique:
                errors["base"] = "modbus_address_conflict"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_build_schema(defaults),
            errors=errors,
        )


class FroniusModbusOptionsFlow(config_entries.OptionsFlow):
    """Handle Fronius Modbus options."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        errors = {}
        defaults = _expand_user_input({}, {**self.config_entry.data, **self.config_entry.options})

        if user_input is not None:
            try:
                validated_input = _expand_user_input(user_input, defaults)
                await validate_input(self.hass, validated_input)
                return self.async_create_entry(
                    title="",
                    data=_entry_payload(validated_input, reconfigure_required=False),
                )
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidPort:
                errors["base"] = "invalid_port"
            except InvalidHost:
                errors["host"] = "invalid_host"
            except ScanIntervalTooShort:
                errors["base"] = "scan_interval_too_short"
            except MissingApiPassword:
                errors["base"] = "missing_api_password"
            except InvalidApiCredentials:
                errors["base"] = "invalid_api_credentials"
            except UnsupportedHardware:
                errors["base"] = "unsupported_hardware"
            except AddressesNotUnique:
                errors["base"] = "modbus_address_conflict"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="init",
            data_schema=_build_schema(defaults),
            errors=errors,
        )

class CannotConnect(exceptions.HomeAssistantError):
    """Error to indicate we cannot connect."""

class InvalidHost(exceptions.HomeAssistantError):
    """Error to indicate there is an invalid hostname."""

class InvalidPort(exceptions.HomeAssistantError):
    """Error to indicate there is an invalid hostname."""

class UnsupportedHardware(exceptions.HomeAssistantError):
    """Error to indicate there is an unsupported hardware."""

class AddressesNotUnique(exceptions.HomeAssistantError):
    """Error to indicate that the modbus addresses are not unique."""

class ScanIntervalTooShort(exceptions.HomeAssistantError):
    """Error to indicate the scan interval is too short."""

class MissingApiPassword(exceptions.HomeAssistantError):
    """Error to indicate the Web API password is required."""

class InvalidApiCredentials(exceptions.HomeAssistantError):
    """Error to indicate Fronius web API credentials are invalid."""
