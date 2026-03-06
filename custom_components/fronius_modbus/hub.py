"""Fronius Modbus Hub."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional
from importlib.metadata import version
from packaging import version as pkg_version

from homeassistant.core import callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .froniusmodbusclient import FroniusModbusClient

from .const import (
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

            return self.hub.data

        except Exception as err:
            raise UpdateFailed(f"Fronius data update failed: {err}")


class Hub:
    """Hub for Fronius Battery Storage Modbus Interface"""

    PYMODBUS_VERSION = '3.11.2'

    def __init__(self, hass: HomeAssistant, name: str, host: str, port: int, inverter_unit_id: int, meter_unit_ids, scan_interval: int) -> None:
        """Init hub."""
        self._hass = hass
        self._name = name
        self._entity_prefix = f'{ENTITY_PREFIX}_{name.lower()}_'

        self._id = f'{name.lower()}_{host.lower().replace('.','')}'
        self.online = True

        self._client = FroniusModbusClient(host=host, port=port, inverter_unit_id=inverter_unit_id, meter_unit_ids=meter_unit_ids, timeout=max(3, (scan_interval - 1)))
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
        close = False,
        read_status_data = False,
        config_entry: ConfigEntry | None = None,
        setup_coordinator: bool = True,
    ):
        """Initialize data and coordinator."""
        await self._hass.async_add_executor_job(self.check_pymodbus_version)
        result = await self._client.init_data()

        if self.storage_configured:
            result : bool = await self._hass.async_add_executor_job(self._client.get_json_storage_info)

        if setup_coordinator:
            # Initialize the coordinator. The config-entry first refresh API
            # is only valid when a config entry is available.
            self.coordinator = FroniusCoordinator(self._hass, self, config_entry=config_entry)
            if config_entry is not None:
                await self.coordinator.async_config_entry_first_refresh()
            else:
                await self.coordinator.async_refresh()

        return

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
        elif mode == 5:
            await self._client.set_grid_discharge_mode()
        elif mode == 6:
            await self._client.set_block_discharge_mode()
        elif mode == 7:
            await self._client.set_block_charge_mode()

    @toggle_busy
    async def set_minimum_reserve(self, value):
        await self._client.set_minimum_reserve(value)

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

    async def set_ac_limit_rate(self, value):
        await self._client.set_ac_limit_rate(value)

    async def set_ac_limit_enable(self, value):
        await self._client.set_ac_limit_enable(value)

    async def set_conn_status(self, enable):
        await self._client.set_conn_status(enable)
