from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries, exceptions
from homeassistant.core import HomeAssistant

from .hub import Hub
from homeassistant.const import CONF_NAME, CONF_HOST, CONF_PORT, CONF_SCAN_INTERVAL
from .const import (
    DOMAIN,
    DEFAULT_NAME,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_INVERTER_UNIT_ID,
    DEFAULT_METER_UNIT_ID,
    CONF_INVERTER_UNIT_ID,
    CONF_METER_UNIT_ID,
    SUPPORTED_MANUFACTURERS,
    SUPPORTED_MODELS,
)

_LOGGER = logging.getLogger(__name__)

DATA_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): str,
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Optional(CONF_INVERTER_UNIT_ID, default=DEFAULT_INVERTER_UNIT_ID): int,
        vol.Optional(CONF_METER_UNIT_ID, default=DEFAULT_METER_UNIT_ID): int,
        vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): int,
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

    all_addresses = meter_addresses + [data[CONF_INVERTER_UNIT_ID]] 

    if len(all_addresses) > len(set(all_addresses)):
        _LOGGER.error(f"Modbus addresses are not unique {all_addresses}")
        raise AddressesNotUnique

    hub = Hub(hass, data[CONF_NAME], data[CONF_HOST], data[CONF_PORT], data[CONF_INVERTER_UNIT_ID], meter_addresses, data[CONF_SCAN_INTERVAL])
    try:
        await hub.init_data(setup_coordinator=False)
    except Exception as e:
        _LOGGER.error(f"Cannot start hub {e}")
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
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}
        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)

                return self.async_create_entry(title=info["title"], data=user_input)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidPort:
                errors["base"] = "invalid_port"
            except InvalidHost:
                errors["host"] = "invalid_host"
            except ScanIntervalTooShort:
                errors["base"] = "scan_interval_too_short"
            except UnsupportedHardware:
                errors["base"] = "unsupported_hardware"
            except AddressesNotUnique:
                errors["base"] = "modbus_address_conflict"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user", data_schema=DATA_SCHEMA, errors=errors
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
