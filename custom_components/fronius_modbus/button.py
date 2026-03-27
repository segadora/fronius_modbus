from homeassistant.components.button import ButtonEntity

from .base import FroniusModbusBaseEntity
from .const import INVERTER_API_BUTTON_TYPES
from .hub import Hub


async def async_setup_entry(hass, config_entry, async_add_entities) -> None:
    del hass
    hub: Hub = config_entry.runtime_data
    coordinator = hub.coordinator

    entities = []
    if hub.web_api_configured:
        for button_info in INVERTER_API_BUTTON_TYPES:
            name, key, icon = button_info[:3]
            entity_category = button_info[3] if len(button_info) > 3 else None
            entities.append(
                FroniusModbusButton(
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


class FroniusModbusButton(FroniusModbusBaseEntity, ButtonEntity):
    """Representation of a Fronius Web API button."""

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

    async def async_press(self) -> None:
        if self._key == "reset_modbus_control":
            await self._hub.reset_modbus_control()

    @property
    def available(self) -> bool:
        return super().available and self._hub.web_api_configured
