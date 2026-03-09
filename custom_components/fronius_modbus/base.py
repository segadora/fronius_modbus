import logging
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.core import callback
from .hub import Hub

_LOGGER = logging.getLogger(__name__)


class FroniusModbusBaseEntity(CoordinatorEntity):
    """Base entity for Fronius Modbus devices."""
    _key = None
    _options_dict = None

    def __init__(self, coordinator, device_info, name, key, device_class=None, state_class=None, unit=None, icon=None, entity_category=None, options=None, min=None, max=None, native_step=None, mode=None):
        """Initialize the entity."""
        super().__init__(coordinator)
        self._key = key
        self._name = name
        self._unit_of_measurement = unit
        self._icon = icon
        self._device_info = device_info

        if device_class is not None:
            self._attr_device_class = device_class
        if state_class is not None:
            self._attr_state_class = state_class
        if entity_category is not None:
            self._attr_entity_category = entity_category
        if options is not None:
            self._options_dict = options
            self._attr_options = list(options.values())
        if unit is not None:
            self._attr_native_unit_of_measurement = unit
        if min is not None:
            self._attr_native_min_value = min
        if max is not None:
            self._attr_native_max_value = max
        if native_step is not None:
            self._attr_native_step = native_step
        if mode is not None:
            self._attr_mode = mode

        self._attr_has_entity_name = True
        self._attr_name = name
        self._attr_unique_id = f"{coordinator.hub.entity_prefix}_{self._key}"
        self._attr_device_info = device_info

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.last_update_success

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass."""
        await super().async_added_to_hass()
        self._handle_coordinator_update()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()

    @property
    def should_poll(self) -> bool:
        """Data is delivered by the coordinator."""
        return False

    @property
    def unit_of_measurement(self):
        """Return the unit of measurement."""
        return self._unit_of_measurement

    @property
    def icon(self):
        """Return the sensor icon."""
        return self._icon

  
