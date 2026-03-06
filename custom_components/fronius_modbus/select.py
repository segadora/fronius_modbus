import logging
from typing import Optional, Dict, Any

from .const import (
    STORAGE_SELECT_TYPES,
    INVERTER_SELECT_TYPES,
)

from homeassistant.core import callback
from homeassistant.const import CONF_NAME
from homeassistant.components.select import (
    SelectEntity,
)

from .hub import Hub
from .base import FroniusModbusBaseEntity

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, config_entry, async_add_entities) -> None:
    hub: Hub = config_entry.runtime_data
    coordinator = hub.coordinator

    entities = []

    if hub.storage_configured:
        for select_info in STORAGE_SELECT_TYPES:
            select = FroniusModbusSelect(
                coordinator=coordinator,
                device_info=hub.device_info_storage,
                name=select_info[0],
                key=select_info[1],
                options=select_info[2],
                hub=hub,  # Pass hub for control methods
            )
            entities.append(select)

    # Add inverter select entities.
    for select_info in INVERTER_SELECT_TYPES:
        select = FroniusModbusSelect(
            coordinator=coordinator,
            device_info=hub.device_info_inverter,
            name=select_info[0],
            key=select_info[1],
            options=select_info[2],
            hub=hub,  # Pass hub for control methods
        )
        entities.append(select)

    async_add_entities(entities)
    return True

def get_key(my_dict, search):
    for k, v in my_dict.items():
        if v == search:
            return k
    return None

class FroniusModbusSelect(FroniusModbusBaseEntity, SelectEntity):
    """Representation of an Battery Storage select."""

    def __init__(self, coordinator, device_info, name, key, options, hub):
        """Initialize the select entity."""
        super().__init__(
            coordinator=coordinator,
            device_info=device_info,
            name=name,
            key=key,
            options=options,
        )
        self._hub = hub  # Store hub reference for control methods

    @property
    def current_option(self) -> str:
        if self.coordinator.data and self._key in self.coordinator.data:
            return self.coordinator.data[self._key]

    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        new_mode = get_key(self._options_dict, option)

        if self._key == 'ext_control_mode':
            await self._hub.set_mode(new_mode)
        elif self._key == 'ac_limit_enable':
            await self._hub.set_ac_limit_enable(new_mode)
        elif self._key == 'Conn':
            await self._hub.set_conn_status(new_mode)

        # Update coordinator data will trigger entity updates
        if self.coordinator.data:
            self.coordinator.data[self._key] = option
        self.async_write_ha_state()
