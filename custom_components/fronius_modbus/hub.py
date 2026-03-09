"""Fronius Modbus Hub."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Optional, Any
from importlib.metadata import version
from packaging import version as pkg_version

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .froniusmodbusclient import FroniusModbusClient
from .froniuswebclient import FroniusWebClient

from .const import (
    API_BATTERY_MODE,
    API_SOC_MODE,
    DOMAIN,
    ENTITY_PREFIX,
)

_LOGGER = logging.getLogger(__name__)


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
        try:
            # Read inverter data
            await self.hub._client.read_inverter_data()

            # Read inverter status data
            await self.hub._client.read_inverter_status_data()

            # Read inverter model settings data
            await self.hub._client.read_inverter_model_settings_data()

            # Read inverter controls data
            await self.hub._client.read_inverter_controls_data()

            # Read meter data if configured
            if self.hub._client.meter_configured:
                for meter_idx, meter_address in enumerate(self.hub._client._meter_unit_ids, start=1):
                    await self.hub._client.read_meter_data(
                        meter_prefix=f"m{meter_idx}_",
                        unit_id=meter_address
                    )

            # Read MPPT data if configured
            if self.hub._client.mppt_configured:
                await self.hub._client.read_mppt_data()

            # Read export limit data
            await self.hub._client.read_export_limit_data()

            # Read storage data if configured
            if self.hub._client.storage_configured:
                await self.hub._client.read_inverter_storage_data()

            # Read authenticated web API data if configured.
            if self.hub.web_api_configured:
                try:
                    await self.hub.refresh_web_data()
                except Exception as err:
                    _LOGGER.warning("Fronius web API refresh failed: %s", err)

            return self.hub.data

        except Exception as err:
            raise UpdateFailed(f"Fronius data update failed: {err}")


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
        if api_username and api_password:
            self._webclient = FroniusWebClient(host=host, username=api_username, password=api_password)
        self._scan_interval = timedelta(seconds=scan_interval)
        self.coordinator = None
        self._busy = False

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

    async def init_data(
        self,
        config_entry: ConfigEntry | None = None,
        setup_coordinator: bool = True,
    ):
        """Initialize data and coordinator."""
        self._config_entry = config_entry
        await self._hass.async_add_executor_job(self.check_pymodbus_version)
        if self.web_api_configured and self._auto_enable_modbus:
            enabled = await self._hass.async_add_executor_job(
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
                storage_info = await self._hass.async_add_executor_job(self._webclient.get_storage_info)
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

    def _apply_web_battery_config(self, battery_config: dict[str, Any]) -> None:
        raw_mode = self._as_int(battery_config.get('HYB_EM_MODE'))
        raw_power = self._as_int(battery_config.get('HYB_EM_POWER'))

        raw_soc_mode = battery_config.get('BAT_M0_SOC_MODE')
        if isinstance(raw_soc_mode, str):
            raw_soc_mode = raw_soc_mode.lower()
        else:
            raw_soc_mode = None

        effective_mode = self._derive_api_battery_mode(raw_mode, raw_soc_mode)

        self.data['api_battery_mode_raw'] = raw_mode
        self.data['api_battery_mode_effective_raw'] = effective_mode
        self.data['api_battery_mode_consistent'] = effective_mode is not None
        self.data['api_battery_mode'] = API_BATTERY_MODE.get(effective_mode) if effective_mode is not None else None
        self.data['api_battery_power'] = -raw_power if raw_power is not None else None
        self.data['api_soc_mode_raw'] = raw_soc_mode
        self.data['api_soc_mode'] = API_SOC_MODE.get(raw_soc_mode, raw_soc_mode)
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

        modbus_config = await self._hass.async_add_executor_job(self._webclient.get_modbus_config)
        self._apply_web_modbus_config(modbus_config)

        if self.storage_configured:
            battery_config = await self._hass.async_add_executor_job(self._webclient.get_battery_config)
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
        await self._hass.async_add_executor_job(
            self._webclient.set_battery_soc_config,
            next_soc_min,
            next_soc_max,
            next_backup_reserved,
        )
        self.data['api_soc_mode_raw'] = 'manual'
        self.data['api_soc_mode'] = API_SOC_MODE['manual']
        self.data['soc_minimum'] = next_soc_min
        self.data['api_soc_min'] = next_soc_min
        self.data['soc_maximum'] = next_soc_max
        self.data['api_backup_reserved'] = next_backup_reserved
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
    
    def get_device_info_meter(self, id) -> dict:
         return {
            "identifiers": {(DOMAIN, f'{self._name}_meter{id}')},
            "name": f'Fronius {self._client.data.get(f'm{id}_model')} {self._client.data.get(f'm{id}_options')}',
            "manufacturer": self._client.data.get(f'm{id}_manufacturer'),
            "model": self._client.data.get(f'm{id}_model'),
            "serial_number": self._client.data.get(f'm{id}_serial'),
            "sw_version": self._client.data.get(f'm{id}_sw_version'),
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
            await self.refresh_web_data()

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

        power = self._as_int(self.data.get('api_battery_power'))
        if mode == 1 and power is None:
            power = 0
        if mode == 1 and power is not None:
            power = -power
        await self._hass.async_add_executor_job(self._webclient.set_battery_config, mode, power if mode == 1 else None)
        await self.refresh_web_data()

    @toggle_busy
    async def set_api_battery_power(self, value: float):
        if not self._webclient:
            return
        self._require_api_battery_mode_manual('Target feed in')

        power = -int(round(value))
        await self._hass.async_add_executor_job(self._webclient.set_battery_config, 1, power)
        await self.refresh_web_data()

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
        await self.refresh_web_data()

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

        await self._hass.async_add_executor_job(
            self._webclient.set_battery_charge_sources,
            next_charge_from_grid,
            next_charge_from_ac,
        )
        await self.refresh_web_data()

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

    async def set_conn_status(self, enable):
        await self._client.set_conn_status(enable)
