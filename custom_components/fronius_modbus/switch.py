from homeassistant.components.switch import SwitchEntity

from .base import FroniusModbusBaseEntity
from .const import INVERTER_API_SWITCH_TYPES, STORAGE_API_SWITCH_TYPES
from .hub import Hub


async def async_setup_entry(hass, config_entry, async_add_entities) -> None:
    del hass
    hub: Hub = config_entry.runtime_data
    coordinator = hub.coordinator

    entities = []

    if hub.storage_configured and hub.web_api_configured:
        for switch_info in STORAGE_API_SWITCH_TYPES:
            name, key, icon = switch_info[:3]
            entity_category = switch_info[3] if len(switch_info) > 3 else None
            entities.append(
                FroniusModbusSwitch(
                    coordinator=coordinator,
                    device_info=hub.device_info_storage,
                    name=name,
                    key=key,
                    icon=icon,
                    entity_category=entity_category,
                    hub=hub,
                )
            )

    if hub.web_api_configured:
        for switch_info in INVERTER_API_SWITCH_TYPES:
            name, key, icon = switch_info[:3]
            entity_category = switch_info[3] if len(switch_info) > 3 else None
            entities.append(
                FroniusModbusSwitch(
                    coordinator=coordinator,
                    device_info=hub.device_info_inverter,
                    name=name,
                    key=key,
                    icon=icon,
                    entity_category=entity_category,
                    hub=hub,
                )
            )

    async_add_entities(entities)
    return True


class FroniusModbusSwitch(FroniusModbusBaseEntity, SwitchEntity):
    """Representation of a Fronius Web API switch."""

    def __init__(self, coordinator, device_info, name, key, icon, hub, entity_category=None):
        super().__init__(
            coordinator=coordinator,
            device_info=device_info,
            name=name,
            key=key,
            icon=icon,
            entity_category=entity_category,
        )
        self._hub = hub

    @property
    def is_on(self):
        value = None
        if self.coordinator.data and self._key in self.coordinator.data:
            value = self.coordinator.data[self._key]
        if value is None:
            return None
        return bool(value)

    async def _async_publish_web_update(self) -> None:
        await self._hub.refresh_web_data()
        if self._hub.coordinator is not None:
            self._hub.coordinator.async_set_updated_data(self._hub.data)

    async def async_turn_on(self, **kwargs) -> None:
        if self._key == "api_charge_from_grid":
            await self._hub.set_api_charge_sources(
                charge_from_grid=True,
                charge_from_ac=True,
            )
        elif self._key == "api_charge_from_ac":
            await self._hub.set_api_charge_sources(charge_from_ac=True)
        elif self._key == "api_solar_api_enabled":
            await self._hub.set_solar_api_enabled(True)
            await self._async_publish_web_update()
            return
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        if self._key == "api_charge_from_grid":
            await self._hub.set_api_charge_sources(charge_from_grid=False)
        elif self._key == "api_charge_from_ac":
            await self._hub.set_api_charge_sources(
                charge_from_grid=False,
                charge_from_ac=False,
            )
        elif self._key == "api_solar_api_enabled":
            await self._hub.set_solar_api_enabled(False)
            await self._async_publish_web_update()
            return
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        if self._key == "api_solar_api_enabled":
            return self._hub.web_api_configured
        return self._hub.web_api_configured and self._hub.storage_configured
