"""Fronius Modbus Hub."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import Any
from importlib.metadata import version
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from packaging import version as pkg_version

from .froniusmodbusclient import FroniusModbusClient
from .froniuswebclient import FroniusWebAuthError, FroniusWebClient

from .const import (
    API_BATTERY_MODE,
    API_SOC_MODE,
    DOMAIN,
    ENTITY_PREFIX,
    API_USERNAME,
    MIGRATION_RECONFIGURE_ISSUE_ID_PREFIX,
)
from .token_store import async_get_token_store

_LOGGER = logging.getLogger(__name__)

WEB_API_DATA_KEYS = (
    "api_modbus_mode",
    "api_modbus_control",
    "api_modbus_sunspec_mode",
    "api_modbus_restriction",
    "api_modbus_restriction_ip",
    "api_battery_mode_raw",
    "api_battery_mode_effective_raw",
    "api_battery_mode_consistent",
    "api_battery_mode",
    "api_battery_power",
    "api_soc_mode_raw",
    "api_soc_mode",
    "api_soc_min",
    "soc_maximum",
    "api_backup_reserved",
    "api_charge_from_ac",
    "api_charge_from_grid",
)

BATTERY_WRITE_MODBUS_RECOVERY_SECONDS = 30.0
BATTERY_WRITE_WEB_REFRESH_DELAY_SECONDS = 10.0


class FroniusCoordinator(DataUpdateCoordinator):
    """Coordinator for Fronius Modbus data updates."""

    def __init__(self, hass: HomeAssistant, hub: Hub, config_entry: ConfigEntry | None = None) -> None:
        """Initialize the coordinator."""
        if config_entry is not None:
            try:
                super().__init__(
                    hass,
                    _LOGGER,
                    name=f"{DOMAIN}_{hub._id}_coordinator",
                    update_interval=hub._scan_interval,
                    config_entry=config_entry,
                )
            except TypeError:
                super().__init__(
                    hass,
                    _LOGGER,
                    name=f"{DOMAIN}_{hub._id}_coordinator",
                    update_interval=hub._scan_interval,
                )
        else:
            super().__init__(
                hass,
                _LOGGER,
                name=f"{DOMAIN}_{hub._id}_coordinator",
                update_interval=hub._scan_interval,
            )
        self.hub = hub

    async def _async_update_data(self) -> dict:
        """Fetch all data from Fronius device."""
        core_err: Exception | None = None
        try:
            core_ok = await self.hub._client.read_inverter_data()
            if not core_ok:
                core_err = RuntimeError("Core inverter read returned no data")
        except Exception as err:
            core_err = err

        if core_err is not None:
            if self.hub._handle_core_modbus_failure(core_err):
                return self.hub.data
            raise UpdateFailed(f"Fronius data update failed: {core_err}")

        self.hub._handle_core_modbus_success()
        await self.hub._async_refresh_optional_data()
        return self.hub.data


class Hub:
    """Hub for Fronius Battery Storage Modbus Interface"""

    PYMODBUS_VERSION = '3.11.2'

    def __init__(
        self,
        hass: HomeAssistant,
        name: str,
        host: str,
        port: int,
        inverter_unit_id: int,
        meter_unit_ids,
        scan_interval: int,
        api_username: str | None = None,
        api_password: str | None = None,
        api_token: dict[str, str] | None = None,
        auto_enable_modbus: bool = True,
        restrict_modbus_to_this_ip: bool = False,
    ) -> None:
        """Init hub."""
        self._hass = hass
        self._name = name
        self._host = host
        self._port = port
        self._inverter_unit_id = inverter_unit_id
        self._meter_unit_ids = meter_unit_ids
        self._entity_prefix = f'{ENTITY_PREFIX}_{name.lower()}_'
        self._config_entry: ConfigEntry | None = None
        self._auto_enable_modbus = auto_enable_modbus
        self._restrict_modbus_to_this_ip = restrict_modbus_to_this_ip
        self._webclient: FroniusWebClient | None = None

        self._id = f'{name.lower()}_{host.lower().replace('.','')}'
        self.online = True

        self._client = FroniusModbusClient(host=host, port=port, inverter_unit_id=inverter_unit_id, meter_unit_ids=meter_unit_ids, timeout=max(3, (scan_interval - 1)))
        if api_username and (api_password or api_token):
            self._webclient = FroniusWebClient(
                host=host,
                username=api_username,
                password=api_password or "",
                token=api_token,
            )
        self._scan_interval = timedelta(seconds=scan_interval)
        self.coordinator = None
        self._busy = False
        self._battery_write_transition_until = 0.0
        self._battery_write_transition_warned = False
        self._delayed_web_refresh_task: asyncio.Task | None = None

    def toggle_busy(func):
        async def wrapper(self, *args, **kwargs):
            if self._busy:
                return
            self._busy = True
            error = None
            try:
                result = await func(self, *args, **kwargs)
            except Exception as e:
                _LOGGER.warning(f'Exception in wrapper {e}')
                error = e
            self._busy = False
            if not error is None:
                raise error
            return result
        return wrapper

    def _meter_prefix(self, unit_id: int) -> str:
        return f"meter_{int(unit_id)}_"

    async def init_data(
        self,
        config_entry: ConfigEntry | None = None,
        setup_coordinator: bool = True,
        apply_modbus_config: bool = False,
    ):
        """Initialize data and coordinator."""
        self._config_entry = config_entry
        await self._hass.async_add_executor_job(self.check_pymodbus_version)
        if apply_modbus_config and self.web_api_configured and self._auto_enable_modbus:
            if len(self._meter_unit_ids) > 1:
                _LOGGER.info(
                    "Applying Modbus web configuration for primary meter %s only; additional configured meters are not programmed via web API",
                    self._meter_unit_ids[0],
                )
            enabled = await self._async_web_job(
                self._webclient.ensure_modbus_enabled,
                self._port,
                self._meter_unit_ids[0] if self._meter_unit_ids else 200,
                self._inverter_unit_id,
                self._restrict_modbus_to_this_ip,
            )
            if enabled:
                await asyncio.sleep(1.0)
        await self._client.init_data()

        if self.storage_configured:
            self._client.reset_storage_info()
            if self.web_api_configured:
                storage_info = await self._async_web_job(self._webclient.get_storage_info)
                if isinstance(storage_info, dict):
                    self._client.set_storage_info(
                        manufacturer=storage_info.get("manufacturer"),
                        model=storage_info.get("model"),
                        serial=storage_info.get("serial"),
                    )

        if self.web_api_configured:
            await self.refresh_web_data()

        if setup_coordinator:
            # Initialize the coordinator. The config-entry first refresh API
            # is only valid when a config entry is available.
            self.coordinator = FroniusCoordinator(self._hass, self, config_entry=config_entry)
            if config_entry is not None:
                await self.coordinator.async_config_entry_first_refresh()
            else:
                await self.coordinator.async_refresh()

        return

    async def validate_web_api(self) -> bool:
        if not self._webclient:
            return False
        return await self._hass.async_add_executor_job(self._webclient.login)

    async def _async_refresh_optional_data(self) -> None:
        await self._async_optional_poll("inverter status", self._client.read_inverter_status_data)
        await self._async_optional_poll("inverter settings", self._client.read_inverter_model_settings_data)
        await self._async_optional_poll("inverter controls", self._client.read_inverter_controls_data)

        if self._client.meter_configured:
            for meter_idx, meter_address in enumerate(self._client._meter_unit_ids, start=1):
                await self._async_optional_poll(
                    f"meter {meter_address}",
                    self._client.read_meter_data,
                    unit_id=meter_address,
                    is_primary=meter_idx == 1,
                )

        if self._client.mppt_configured:
            await self._async_optional_poll("mppt", self._client.read_mppt_data)

        await self._async_optional_poll("ac limit", self._client.read_export_limit_data)

        if self._client.storage_configured:
            await self._async_optional_poll("storage", self._client.read_inverter_storage_data)

        if self.web_api_configured:
            try:
                await self.refresh_web_data()
            except Exception as err:
                _LOGGER.warning("Fronius web API refresh failed: %s", err)

    async def _async_optional_poll(self, label: str, func, *args, **kwargs) -> bool:
        try:
            result = await func(*args, **kwargs)
        except Exception as err:
            _LOGGER.warning("Optional Fronius %s refresh failed: %s", label, err)
            return False

        if result is False:
            _LOGGER.debug("Optional Fronius %s refresh returned no data", label)
            return False
        return True

    def _clear_web_api_data(self) -> None:
        for key in WEB_API_DATA_KEYS:
            self.data[key] = None

    def _battery_write_transition_active(self) -> bool:
        return time.monotonic() < self._battery_write_transition_until

    def _clear_battery_write_transition(self) -> None:
        self._battery_write_transition_until = 0.0
        self._battery_write_transition_warned = False

    def _schedule_delayed_web_refresh(self) -> None:
        if self._delayed_web_refresh_task and not self._delayed_web_refresh_task.done():
            self._delayed_web_refresh_task.cancel()

        async def delayed_refresh() -> None:
            try:
                await asyncio.sleep(BATTERY_WRITE_WEB_REFRESH_DELAY_SECONDS)
                if self._webclient:
                    await self.refresh_web_data()
            except asyncio.CancelledError:
                raise
            except Exception as err:
                _LOGGER.warning("Delayed Fronius web API refresh failed: %s", err)

        self._delayed_web_refresh_task = self._hass.loop.create_task(delayed_refresh())

    def _start_battery_write_transition(self, source: str) -> None:
        self._battery_write_transition_until = (
            time.monotonic() + BATTERY_WRITE_MODBUS_RECOVERY_SECONDS
        )
        self._battery_write_transition_warned = False
        self._client.close()
        self._schedule_delayed_web_refresh()
        _LOGGER.debug(
            "Started Modbus recovery window after %s write for %s",
            source,
            self._host,
        )

    def _handle_core_modbus_success(self) -> None:
        if self._battery_write_transition_active():
            _LOGGER.debug("Modbus recovered after battery API write on %s", self._host)
        self._clear_battery_write_transition()

    def _handle_core_modbus_failure(self, err: Exception) -> bool:
        if not self._battery_write_transition_active():
            return False

        if not self._battery_write_transition_warned:
            _LOGGER.warning(
                "Suppressing temporary Modbus outage after battery API write on %s: %s",
                self._host,
                err,
            )
            self._battery_write_transition_warned = True
        else:
            _LOGGER.debug(
                "Modbus still recovering after battery API write on %s: %s",
                self._host,
                err,
            )
        return True

    def _set_effective_api_battery_mode(
        self,
        raw_mode: int | None,
        raw_soc_mode: str | None,
    ) -> None:
        effective_mode = self._derive_api_battery_mode(raw_mode, raw_soc_mode)
        self.data['api_battery_mode_raw'] = raw_mode
        self.data['api_battery_mode_effective_raw'] = effective_mode
        self.data['api_battery_mode_consistent'] = effective_mode is not None
        self.data['api_battery_mode'] = (
            API_BATTERY_MODE.get(effective_mode) if effective_mode is not None else None
        )
        self.data['api_soc_mode_raw'] = raw_soc_mode
        self.data['api_soc_mode'] = API_SOC_MODE.get(raw_soc_mode, raw_soc_mode)

    async def _async_handle_web_api_auth_failure(self, err: Exception) -> None:
        if not self._webclient:
            return

        _LOGGER.warning("Disabling Fronius web API for %s after auth failure: %s", self._host, err)
        self._webclient = None
        self._clear_web_api_data()
        await async_get_token_store(self._hass).async_delete_token(self._host, API_USERNAME)

        if self._config_entry is not None:
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                f"{MIGRATION_RECONFIGURE_ISSUE_ID_PREFIX}{self._config_entry.entry_id}",
                is_fixable=True,
                is_persistent=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key="legacy_modbus_only_entry_reconfigure",
                translation_placeholders={"entry_title": self._config_entry.title or self._name},
                data={"entry_id": self._config_entry.entry_id},
            )

    async def _async_web_job(
        self,
        func,
        *args,
        raise_on_auth_failure: bool = False,
    ):
        if not self._webclient:
            if raise_on_auth_failure:
                raise RuntimeError("Fronius Web API is not configured")
            return None

        try:
            return await self._hass.async_add_executor_job(func, *args)
        except FroniusWebAuthError as err:
            await self._async_handle_web_api_auth_failure(err)
            if raise_on_auth_failure:
                raise RuntimeError(
                    "Fronius Web API authentication failed. Reconfigure the integration."
                ) from err
            return None

    def _apply_web_battery_config(self, battery_config: dict[str, Any]) -> None:
        raw_mode = self._as_int(battery_config.get('HYB_EM_MODE'))
        raw_power = self._as_int(battery_config.get('HYB_EM_POWER'))

        raw_soc_mode = battery_config.get('BAT_M0_SOC_MODE')
        if isinstance(raw_soc_mode, str):
            raw_soc_mode = raw_soc_mode.lower()
        else:
            raw_soc_mode = None

        effective_mode = self._derive_api_battery_mode(raw_mode, raw_soc_mode)
        self._set_effective_api_battery_mode(raw_mode, raw_soc_mode)
        self.data['api_battery_power'] = -raw_power if raw_power is not None else None
        api_soc_min = self._as_int(battery_config.get('BAT_M0_SOC_MIN'))
        self.data['api_soc_min'] = api_soc_min
        self.data['soc_maximum'] = self._as_int(battery_config.get('BAT_M0_SOC_MAX'))
        self.data['api_backup_reserved'] = self._as_int(battery_config.get('HYB_BACKUP_RESERVED'))
        if effective_mode == 1 and api_soc_min is not None:
            self.data['soc_minimum'] = api_soc_min
        self.data['api_charge_from_ac'] = self._enabled_bool(battery_config.get('HYB_BM_CHARGEFROMAC'))
        self.data['api_charge_from_grid'] = self._enabled_bool(battery_config.get('HYB_EVU_CHARGEFROMGRID'))

    def _apply_web_modbus_config(self, modbus_config: dict[str, Any]) -> None:
        slave = modbus_config.get('slave') or {}
        ctr = slave.get('ctr') or {}
        restriction = ctr.get('restriction') or {}
        mode = slave.get('mode')

        self.data['api_modbus_mode'] = str(mode).upper() if mode is not None else None
        self.data['api_modbus_control'] = self._enabled_state(ctr.get('on'))
        self.data['api_modbus_sunspec_mode'] = slave.get('sunspecMode')
        self.data['api_modbus_restriction'] = self._enabled_state(restriction.get('on'))
        self.data['api_modbus_restriction_ip'] = restriction.get('ip')

    async def refresh_web_data(self) -> None:
        if not self._webclient:
            return

        modbus_config = await self._async_web_job(self._webclient.get_modbus_config)
        if not isinstance(modbus_config, dict):
            return
        self._apply_web_modbus_config(modbus_config)

        if self.storage_configured:
            battery_config = await self._async_web_job(self._webclient.get_battery_config)
            if not isinstance(battery_config, dict):
                return
            self._apply_web_battery_config(battery_config)

    def _get_next_soc_limits(
        self,
        *,
        soc_min: int | None = None,
        soc_max: int | None = None,
    ) -> tuple[int, int]:
        next_soc_min = self._as_int(self.data.get('soc_minimum')) if soc_min is None else int(soc_min)
        next_soc_max = self._as_int(self.data.get('soc_maximum')) if soc_max is None else int(soc_max)

        next_soc_min = 5 if next_soc_min is None else next_soc_min
        next_soc_max = 99 if next_soc_max is None else next_soc_max

        if next_soc_min < 5 or next_soc_min > 100:
            raise ValueError('SoC Minimum must be between 5 and 100')
        if next_soc_max < 0 or next_soc_max > 100:
            raise ValueError('SoC Maximum must be between 0 and 100')
        if next_soc_min > next_soc_max:
            raise ValueError('SoC Minimum must not exceed SoC Maximum')

        return next_soc_min, next_soc_max

    def _get_api_soc_values(
        self,
        *,
        soc_min: int | None = None,
        soc_max: int | None = None,
    ) -> tuple[int, int, int]:
        next_soc_min, next_soc_max = self._get_next_soc_limits(
            soc_min=soc_min,
            soc_max=soc_max,
        )
        next_backup_reserved = self._as_int(self.data.get('api_backup_reserved'))
        next_backup_reserved = 5 if next_backup_reserved is None else next_backup_reserved
        if next_backup_reserved < 5 or next_backup_reserved > 100:
            raise ValueError('Battery backup reserve must be between 5 and 100')

        return next_soc_min, next_soc_max, next_backup_reserved

    def _derive_api_battery_mode(
        self,
        raw_mode: int | None,
        raw_soc_mode: str | None,
    ) -> int | None:
        if raw_mode == 1 and raw_soc_mode == 'manual':
            return 1
        if raw_mode == 0 and raw_soc_mode == 'auto':
            return 0
        return None

    def _api_battery_mode_is_manual(self) -> bool:
        return self._as_int(self.data.get('api_battery_mode_effective_raw')) == 1

    def _require_api_battery_mode_manual(self, control_name: str) -> None:
        if not self._api_battery_mode_is_manual():
            raise ValueError(f'{control_name} can only be changed when Battery API mode is Manual')

    async def _set_api_soc_manual(
        self,
        soc_min: int | None = None,
        soc_max: int | None = None,
        control_name: str = 'SoC Maximum',
    ) -> tuple[int, int, int] | None:
        if not self._webclient:
            return None
        self._require_api_battery_mode_manual(control_name)

        next_soc_min, next_soc_max, next_backup_reserved = self._get_api_soc_values(
            soc_min=soc_min,
            soc_max=soc_max,
        )
        await self._async_web_job(
            self._webclient.set_battery_soc_config,
            next_soc_min,
            next_soc_max,
            next_backup_reserved,
            raise_on_auth_failure=True,
        )
        self._set_effective_api_battery_mode(1, 'manual')
        self.data['soc_minimum'] = next_soc_min
        self.data['api_soc_min'] = next_soc_min
        self.data['soc_maximum'] = next_soc_max
        self.data['api_backup_reserved'] = next_backup_reserved
        self._start_battery_write_transition(control_name)
        return next_soc_min, next_soc_max, next_backup_reserved

    def check_pymodbus_version(self):
        try:
            current_version = version('pymodbus')
            if current_version is None:
                _LOGGER.warning(f"pymodbus not found")
                return

            current = pkg_version.parse(current_version)
            required = pkg_version.parse(self.PYMODBUS_VERSION)

            if current < required:
                raise Exception(f"pymodbus {current_version} found, please update to {self.PYMODBUS_VERSION} or higher")
            elif current > required:
                _LOGGER.warning(f"newer pymodbus {current_version} found")
            _LOGGER.debug(f"pymodbus {current_version}")
        except Exception as e:
            _LOGGER.error(f"Error checking pymodbus version: {e}")
            raise

    def _as_int(self, value: Any) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _require_whole_number(self, value: Any, field_name: str) -> int:
        try:
            numeric_value = float(value)
        except (TypeError, ValueError) as err:
            raise ValueError(f"{field_name} must be a whole number") from err

        if not numeric_value.is_integer():
            raise ValueError(f"{field_name} must be a whole number")

        return int(numeric_value)

    def _enabled_state(self, value: Any) -> str:
        if isinstance(value, str):
            normalized = value.strip().lower()
            is_enabled = normalized in ['1', 'true', 'on', 'yes', 'enabled']
        else:
            is_enabled = bool(value)
        return 'Enabled' if is_enabled else 'Disabled'

    def _enabled_bool(self, value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in ['1', 'true', 'on', 'yes', 'enabled']
        return bool(value)

    @property 
    def device_info_storage(self) -> dict:
        return {
            "identifiers": {(DOMAIN, f'{self._name}_battery_storage')},
            "name": f'{self._client.data.get('s_model')}',
            "manufacturer": self._client.data.get('s_manufacturer'),
            "model": self._client.data.get('s_model'),
            "serial_number": self._client.data.get('s_serial'),
        }

    @property 
    def device_info_inverter(self) -> dict:
        return {
            "identifiers": {(DOMAIN, f'{self._name}_inverter')},
            "name": f'Fronius {self._client.data.get('i_model')}',
            "manufacturer": self._client.data.get('i_manufacturer'),
            "model": self._client.data.get('i_model'),
            "serial_number": self._client.data.get('i_serial'),
            "sw_version": self._client.data.get('i_sw_version'),
        }
    
    def get_device_info_meter(self, unit_id: int) -> dict:
        prefix = self._meter_prefix(unit_id)
        return {
            "identifiers": {(DOMAIN, f'{self._name}_meter_{unit_id}')},
            "name": f'Fronius {self._client.data.get(f"{prefix}model")} {self._client.data.get(f"{prefix}options")}',
            "manufacturer": self._client.data.get(f"{prefix}manufacturer"),
            "model": self._client.data.get(f"{prefix}model"),
            "serial_number": self._client.data.get(f"{prefix}serial"),
            "sw_version": self._client.data.get(f"{prefix}sw_version"),
        }

    @property
    def hub_id(self) -> str:
        """ID for hub."""
        return self._id

    @property
    def entity_prefix(self) -> str:
        """Entity prefix for hub."""
        return self._entity_prefix



    def close(self):
        """Disconnect client."""
        if self._delayed_web_refresh_task and not self._delayed_web_refresh_task.done():
            self._delayed_web_refresh_task.cancel()
        self._client.close()

    @property
    def data(self):
        return self._client.data

    @property
    def web_api_configured(self) -> bool:
        return self._webclient is not None

    @property
    def meter_configured(self):
        return self._client.meter_configured

    @property
    def storage_configured(self):
        return self._client.storage_configured

    @property
    def max_discharge_rate_w(self):
        return self._client.max_discharge_rate_w

    @property
    def max_charge_rate_w(self):
        return self._client.max_charge_rate_w

    @property
    def storage_extended_control_mode(self):
        return self._client.storage_extended_control_mode

    @toggle_busy
    async def set_mode(self, mode):
        if mode == 0:
            await self._client.set_auto_mode()
        elif mode == 1:
            await self._client.set_charge_mode()
        elif mode == 2:
            await self._client.set_discharge_mode()
        elif mode == 3:
            await self._client.set_charge_discharge_mode()
        elif mode == 4:
            await self._client.set_grid_charge_mode()
            if self._webclient:
                try:
                    await self._set_api_charge_sources(
                        charge_from_grid=True,
                        charge_from_ac=True,
                    )
                except Exception as err:
                    _LOGGER.warning(
                        "Failed enabling Web API charge-source toggles after Modbus Charge from Grid: %s",
                        err,
                    )
        elif mode == 5:
            await self._client.set_grid_discharge_mode()
        elif mode == 6:
            await self._client.set_block_discharge_mode()
        elif mode == 7:
            await self._client.set_block_charge_mode()

    @toggle_busy
    async def set_soc_minimum(self, value):
        soc_minimum = self._require_whole_number(value, 'SoC Minimum')
        if soc_minimum < 5 or soc_minimum > 100:
            raise ValueError('SoC Minimum must be between 5 and 100')
        if self._webclient and self._api_battery_mode_is_manual():
            self._get_next_soc_limits(soc_min=soc_minimum)
        await self._client.set_minimum_reserve(soc_minimum)
        self.data['soc_minimum'] = soc_minimum
        if self._webclient and self._api_battery_mode_is_manual():
            await self._set_api_soc_manual(soc_min=soc_minimum, control_name='SoC Minimum')

    @toggle_busy
    async def set_charge_limit(self, value):
        await self._client.set_charge_limit(value)

    @toggle_busy
    async def set_discharge_limit(self, value):
        await self._client.set_discharge_limit(value)

    @toggle_busy
    async def set_grid_charge_power(self, value):
        await self._client.set_grid_charge_power(value)
           
    @toggle_busy
    async def set_grid_discharge_power(self, value):
        await self._client.set_grid_discharge_power(value)

    @toggle_busy
    async def set_api_battery_mode(self, mode: int):
        if not self._webclient:
            return

        current_effective_mode = self._as_int(self.data.get('api_battery_mode_effective_raw'))
        display_power = self._as_int(self.data.get('api_battery_power'))
        if mode == 1 and display_power is None:
            display_power = 0
        power = -display_power if mode == 1 and display_power is not None else None
        soc_min = None
        if mode == 1 and current_effective_mode != 1:
            soc_min = self._as_int(self.data.get('soc_minimum'))
        await self._async_web_job(
            self._webclient.set_battery_config,
            mode,
            power,
            soc_min,
            raise_on_auth_failure=True,
        )
        self._set_effective_api_battery_mode(mode, 'manual' if mode == 1 else 'auto')
        if mode == 1:
            self.data['api_battery_power'] = display_power
            if soc_min is not None:
                self.data['api_soc_min'] = soc_min
        else:
            self.data['api_soc_min'] = 5
            self.data['soc_maximum'] = 100
        self._start_battery_write_transition('Battery API mode')

    @toggle_busy
    async def set_api_battery_power(self, value: float):
        if not self._webclient:
            return
        self._require_api_battery_mode_manual('Target feed in')

        power = -int(round(value))
        await self._async_web_job(
            self._webclient.set_battery_config,
            1,
            power,
            raise_on_auth_failure=True,
        )
        self.data['api_battery_power'] = int(round(value))
        self._set_effective_api_battery_mode(1, 'manual')
        self._start_battery_write_transition('Target feed in')

    @toggle_busy
    async def set_api_soc_values(
        self,
        soc_max: int | None = None,
    ):
        if not self._webclient:
            return

        await self._set_api_soc_manual(
            soc_max=soc_max,
            control_name='SoC Maximum',
        )

    async def _set_api_charge_sources(
        self,
        *,
        charge_from_grid: bool | None = None,
        charge_from_ac: bool | None = None,
    ) -> None:
        if not self._webclient:
            return

        if charge_from_ac is False:
            next_charge_from_grid = False
            next_charge_from_ac = False
        else:
            next_charge_from_grid = (
                self._enabled_bool(self.data.get('api_charge_from_grid'))
                if charge_from_grid is None
                else bool(charge_from_grid)
            )
            next_charge_from_ac = (
                self._enabled_bool(self.data.get('api_charge_from_ac'))
                if charge_from_ac is None
                else bool(charge_from_ac)
            )
            if next_charge_from_grid and charge_from_ac is None:
                next_charge_from_ac = True

        await self._async_web_job(
            self._webclient.set_battery_charge_sources,
            next_charge_from_grid,
            next_charge_from_ac,
            raise_on_auth_failure=True,
        )
        self.data['api_charge_from_grid'] = next_charge_from_grid
        self.data['api_charge_from_ac'] = next_charge_from_ac
        self._start_battery_write_transition('battery charge source')

    @toggle_busy
    async def set_api_charge_sources(
        self,
        *,
        charge_from_grid: bool | None = None,
        charge_from_ac: bool | None = None,
    ) -> None:
        await self._set_api_charge_sources(
            charge_from_grid=charge_from_grid,
            charge_from_ac=charge_from_ac,
        )

    async def set_ac_limit_rate(self, value):
        await self._client.set_ac_limit_rate(value)

    async def set_ac_limit_enable(self, value):
        await self._client.set_ac_limit_enable(value)

    @toggle_busy
    async def set_power_factor(self, value):
        await self._client.set_power_factor(value)

    @toggle_busy
    async def set_power_factor_enable(self, value):
        await self._client.set_power_factor_enable(value)

    async def set_conn_status(self, enable):
        await self._client.set_conn_status(enable)
