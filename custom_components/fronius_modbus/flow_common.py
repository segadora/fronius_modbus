from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import voluptuous as vol

from homeassistant import config_entries, exceptions
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from .const import (
    CONF_API_PASSWORD,
    CONF_API_USERNAME,
    CONF_AUTO_ENABLE_MODBUS,
    CONF_INVERTER_UNIT_ID,
    CONF_METER_UNIT_ID,
    CONF_RECONFIGURE_REQUIRED,
    CONF_RESTRICT_MODBUS_TO_THIS_IP,
    DEFAULT_AUTO_ENABLE_MODBUS,
    DEFAULT_INVERTER_UNIT_ID,
    DEFAULT_METER_UNIT_ID,
    DEFAULT_NAME,
    DEFAULT_PORT,
    DEFAULT_RESTRICT_MODBUS_TO_THIS_IP,
    DEFAULT_SCAN_INTERVAL,
    FIXED_API_USERNAME,
    SUPPORTED_MANUFACTURERS,
    SUPPORTED_MODELS,
)
from .froniuswebclient import ClientIpResolutionError, mint_token
from .hub import Hub
from .token_store import async_get_token_store

_LOGGER = logging.getLogger(__name__)

type FlowFinishCallback = Callable[
    [dict[str, Any], dict[str, Any], str | None],
    Awaitable[Any],
]
type FlowRestartCallback = Callable[[], Awaitable[Any]]


@dataclass(slots=True)
class PendingFlowState:
    settings: dict[str, Any]
    previous_host: str | None


def default_payload() -> dict[str, Any]:
    return {
        CONF_NAME: DEFAULT_NAME,
        CONF_HOST: "",
        CONF_PORT: DEFAULT_PORT,
        CONF_INVERTER_UNIT_ID: DEFAULT_INVERTER_UNIT_ID,
        CONF_METER_UNIT_ID: DEFAULT_METER_UNIT_ID,
        CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
        CONF_API_USERNAME: FIXED_API_USERNAME,
        CONF_AUTO_ENABLE_MODBUS: DEFAULT_AUTO_ENABLE_MODBUS,
        CONF_RESTRICT_MODBUS_TO_THIS_IP: DEFAULT_RESTRICT_MODBUS_TO_THIS_IP,
    }


def expand_settings_input(
    user_input: dict[str, Any],
    defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = default_payload()
    if defaults:
        payload.update(defaults)
    payload[CONF_HOST] = str(user_input.get(CONF_HOST, payload[CONF_HOST])).strip()
    payload[CONF_SCAN_INTERVAL] = int(user_input.get(CONF_SCAN_INTERVAL, payload[CONF_SCAN_INTERVAL]))
    payload[CONF_RESTRICT_MODBUS_TO_THIS_IP] = bool(
        user_input.get(
            CONF_RESTRICT_MODBUS_TO_THIS_IP,
            payload[CONF_RESTRICT_MODBUS_TO_THIS_IP],
        )
    )
    payload[CONF_API_USERNAME] = FIXED_API_USERNAME
    payload.pop(CONF_API_PASSWORD, None)
    return payload


def entry_payload(data: dict[str, Any], *, reconfigure_required: bool) -> dict[str, Any]:
    payload = dict(data)
    payload.pop(CONF_API_PASSWORD, None)
    payload[CONF_RECONFIGURE_REQUIRED] = reconfigure_required
    return payload


def entry_defaults(entry: config_entries.ConfigEntry) -> dict[str, Any]:
    return expand_settings_input({}, {**entry.data, **entry.options})


def build_settings_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
            vol.Required(
                CONF_SCAN_INTERVAL,
                default=defaults.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            ): vol.Coerce(int),
            vol.Required(
                CONF_RESTRICT_MODBUS_TO_THIS_IP,
                default=defaults.get(
                    CONF_RESTRICT_MODBUS_TO_THIS_IP,
                    DEFAULT_RESTRICT_MODBUS_TO_THIS_IP,
                ),
            ): bool,
        }
    )


def build_password_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_API_PASSWORD): TextSelector(
                TextSelectorConfig(
                    type=TextSelectorType.PASSWORD,
                    autocomplete="current-password",
                )
            )
        }
    )


def set_form_error(errors: dict[str, str], err: Exception) -> None:
    if isinstance(err, CannotConnect):
        errors["base"] = "cannot_connect"
    elif isinstance(err, InvalidPort):
        errors["base"] = "invalid_port"
    elif isinstance(err, InvalidHost):
        errors["host"] = "invalid_host"
    elif isinstance(err, ScanIntervalTooShort):
        errors["base"] = "scan_interval_too_short"
    elif isinstance(err, MissingApiPassword):
        errors["base"] = "missing_api_password"
    elif isinstance(err, InvalidApiCredentials):
        errors["base"] = "invalid_api_credentials"
    elif isinstance(err, CannotResolveLocalIp):
        errors["base"] = "cannot_resolve_local_ip"
    elif isinstance(err, UnsupportedHardware):
        errors["base"] = "unsupported_hardware"
    elif isinstance(err, AddressesNotUnique):
        errors["base"] = "modbus_address_conflict"
    else:
        _LOGGER.exception("Unexpected exception")
        errors["base"] = "unknown"


def validate_static_input(data: dict[str, Any]) -> None:
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
        _LOGGER.error("Modbus addresses are not unique %s", all_addresses)
        raise AddressesNotUnique


async def async_load_token(hass: HomeAssistant, host: str) -> dict[str, str] | None:
    return await async_get_token_store(hass).async_load_token(host, FIXED_API_USERNAME)


async def async_save_token(hass: HomeAssistant, host: str, token: dict[str, str]) -> None:
    await async_get_token_store(hass).async_save_token(
        host,
        realm=token["realm"],
        token=token["token"],
        user=FIXED_API_USERNAME,
    )


async def async_delete_token(hass: HomeAssistant, host: str | None) -> None:
    if host:
        await async_get_token_store(hass).async_delete_token(host, FIXED_API_USERNAME)


async def async_mint_token(
    hass: HomeAssistant,
    host: str,
    password: str,
) -> dict[str, str]:
    password = str(password).strip()
    if password == "":
        raise MissingApiPassword

    try:
        token = await hass.async_add_executor_job(
            mint_token,
            host,
            FIXED_API_USERNAME,
            password,
        )
    except Exception as err:
        raise CannotConnect from err

    if not token:
        raise InvalidApiCredentials
    return token


async def validate_input(
    hass: HomeAssistant,
    data: dict[str, Any],
    *,
    api_password: str = "",
    api_token: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Validate the user input allows us to connect."""
    validate_static_input(data)

    if not api_password and api_token is None:
        raise MissingApiPassword

    if data[CONF_METER_UNIT_ID] > 0:
        meter_addresses = [data[CONF_METER_UNIT_ID]]
    else:
        meter_addresses = []

    hub = Hub(
        hass,
        data[CONF_NAME],
        data[CONF_HOST],
        data[CONF_PORT],
        data[CONF_INVERTER_UNIT_ID],
        meter_addresses,
        data[CONF_SCAN_INTERVAL],
        api_username=FIXED_API_USERNAME,
        api_password=api_password or None,
        api_token=api_token,
        auto_enable_modbus=data.get(CONF_AUTO_ENABLE_MODBUS, DEFAULT_AUTO_ENABLE_MODBUS),
        restrict_modbus_to_this_ip=data.get(
            CONF_RESTRICT_MODBUS_TO_THIS_IP,
            DEFAULT_RESTRICT_MODBUS_TO_THIS_IP,
        ),
    )
    try:
        if not await hub.validate_web_api():
            raise InvalidApiCredentials
        await hub.init_data(setup_coordinator=False)
    except ClientIpResolutionError:
        raise CannotResolveLocalIp
    except InvalidApiCredentials:
        raise
    except Exception as err:
        _LOGGER.error("Cannot start hub %s", err)
        raise CannotConnect from err
    finally:
        hub.close()

    manufacturer = hub.data.get("i_manufacturer")
    if manufacturer is None:
        _LOGGER.error("No manufacturer is returned")
        raise UnsupportedHardware
    if manufacturer not in SUPPORTED_MANUFACTURERS:
        _LOGGER.error("Unsupported manufacturer: %r", manufacturer)
        raise UnsupportedHardware

    model = hub.data.get("i_model")
    if model is None:
        _LOGGER.error("No model type is returned")
        raise UnsupportedHardware

    if not any(model.startswith(supported_model) for supported_model in SUPPORTED_MODELS):
        _LOGGER.warning("Untested model %s", model)

    return {"title": data[CONF_NAME]}


async def async_update_entry_from_input(
    hass: HomeAssistant,
    entry: config_entries.ConfigEntry,
    validated_input: dict[str, Any],
    *,
    previous_host: str | None = None,
) -> None:
    updated_payload = entry_payload(validated_input, reconfigure_required=False)
    new_data = {**entry.data, **updated_payload}
    new_options = {**entry.options, **updated_payload}
    new_data.pop(CONF_API_PASSWORD, None)
    new_options.pop(CONF_API_PASSWORD, None)
    hass.config_entries.async_update_entry(
        entry,
        data=new_data,
        options=new_options,
        title=validated_input[CONF_NAME],
    )
    if previous_host and previous_host != validated_input[CONF_HOST]:
        await async_delete_token(hass, previous_host)
    await hass.config_entries.async_reload(entry.entry_id)


class TokenFlowMixin:
    _pending_flow_state: PendingFlowState | None

    async def _async_show_password_step(
        self,
        *,
        step_id: str,
        errors: dict[str, str] | None = None,
    ):
        return self.async_show_form(
            step_id=step_id,
            data_schema=build_password_schema(),
            errors=errors or {},
        )

    async def _async_handle_settings_step(
        self,
        *,
        user_input: dict[str, Any] | None,
        step_id: str,
        password_step_id: str,
        defaults: dict[str, Any],
        previous_host: str | None,
        on_success: FlowFinishCallback,
    ):
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                settings = expand_settings_input(user_input, defaults)
                validate_static_input(settings)
                token = await async_load_token(self.hass, settings[CONF_HOST])
                if token is None:
                    self._pending_flow_state = PendingFlowState(settings, previous_host)
                    return await self._async_show_password_step(step_id=password_step_id)

                info = await validate_input(self.hass, settings, api_token=token)
                self._pending_flow_state = None
                return await on_success(settings, info, previous_host)
            except InvalidApiCredentials:
                self._pending_flow_state = PendingFlowState(settings, previous_host)
                return await self._async_show_password_step(step_id=password_step_id)
            except Exception as err:  # pylint: disable=broad-except
                set_form_error(errors, err)

        return self.async_show_form(
            step_id=step_id,
            data_schema=build_settings_schema(defaults),
            errors=errors,
        )

    async def _async_handle_password_step(
        self,
        *,
        user_input: dict[str, Any] | None,
        step_id: str,
        restart_step: FlowRestartCallback,
        on_success: FlowFinishCallback,
    ):
        errors: dict[str, str] = {}
        state = self._pending_flow_state
        if state is None:
            return await restart_step()

        if user_input is not None:
            try:
                token = await async_mint_token(
                    self.hass,
                    state.settings[CONF_HOST],
                    user_input.get(CONF_API_PASSWORD, ""),
                )
                await async_save_token(self.hass, state.settings[CONF_HOST], token)
                info = await validate_input(self.hass, state.settings, api_token=token)
                self._pending_flow_state = None
                return await on_success(state.settings, info, state.previous_host)
            except Exception as err:  # pylint: disable=broad-except
                set_form_error(errors, err)

        return await self._async_show_password_step(step_id=step_id, errors=errors)


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


class CannotResolveLocalIp(exceptions.HomeAssistantError):
    """Error to indicate the local IP for Modbus restriction cannot be resolved."""
