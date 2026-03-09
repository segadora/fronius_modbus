import logging

from homeassistant.components.switch import SwitchEntity

from .const import STORAGE_API_SWITCH_TYPES
from .hub import Hub
from .base import FroniusModbusBaseEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry, async_add_entities) -> None:
    hub: Hub = config_entry.runtime_data
    coordinator = hub.coordinator

    entities = []

    if hub.storage_configured and hub.web_api_configured:
        for switch_info in STORAGE_API_SWITCH_TYPES:
            switch = FroniusModbusSwitch(
                coordinator=coordinator,
                device_info=hub.device_info_storage,
                name=switch_info[0],
                key=switch_info[1],
                icon=switch_info[2],
                hub=hub,
            )
            entities.append(switch)

    async_add_entities(entities)
    return True


class FroniusModbusSwitch(FroniusModbusBaseEntity, SwitchEntity):
    """Representation of a Fronius Web API switch."""

    def __init__(self, coordinator, device_info, name, key, icon, hub):
        super().__init__(
            coordinator=coordinator,
            device_info=device_info,
            name=name,
            key=key,
            icon=icon,
        )
        self._hub = hub

    @property
    def is_on(self):
        if self.coordinator.data and self._key in self.coordinator.data:
            return bool(self.coordinator.data[self._key])
        return None

    async def async_turn_on(self, **kwargs) -> None:
        if self._key == 'api_charge_from_grid':
            await self._hub.set_api_charge_sources(
                charge_from_grid=True,
                charge_from_ac=True,
            )
        elif self._key == 'api_charge_from_ac':
            await self._hub.set_api_charge_sources(charge_from_ac=True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        if self._key == 'api_charge_from_grid':
            await self._hub.set_api_charge_sources(charge_from_grid=False)
        elif self._key == 'api_charge_from_ac':
            await self._hub.set_api_charge_sources(
                charge_from_grid=False,
                charge_from_ac=False,
            )
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return self._hub.web_api_configured and self._hub.storage_configured
