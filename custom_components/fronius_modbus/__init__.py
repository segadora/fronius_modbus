"""The Fronius Modbus integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import Platform
from homeassistant.helpers import entity_registry as er

from homeassistant.const import CONF_NAME, CONF_HOST, CONF_PORT, CONF_SCAN_INTERVAL
from .const import (
    DOMAIN,
    CONF_INVERTER_UNIT_ID,
    CONF_METER_UNIT_ID,
)

from . import hub

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.NUMBER, Platform.SELECT, Platform.SENSOR]

type HubConfigEntry = ConfigEntry[hub.Hub]

LEGACY_MPPT_ENTITY_KEYS = (
    "mppt1_current",
    "mppt1_voltage",
    "mppt1_power",
    "mppt1_lfte",
    "mppt2_current",
    "mppt2_voltage",
    "mppt2_power",
    "mppt2_lfte",
    "mppt3_current",
    "mppt3_voltage",
    "mppt3_pv_power",
    "mppt3_pv_lfte",
    "mppt3_power",
    "mppt4_power",
    "mppt3_lfte",
    "mppt4_lfte",
)

LEGACY_RENAMED_ENTITY_KEYS = (
    "export_limit_rate",
    "export_limit_enable",
)


def _is_legacy_mppt_unique_id(unique_id: str) -> bool:
    return any(unique_id.endswith(f"_{key}") for key in LEGACY_MPPT_ENTITY_KEYS)


def _is_legacy_renamed_unique_id(unique_id: str) -> bool:
    return any(unique_id.endswith(f"_{key}") for key in LEGACY_RENAMED_ENTITY_KEYS)


async def _async_remove_legacy_mppt_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    registry = er.async_get(hass)
    removed = 0
    for entity_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        unique_id = entity_entry.unique_id or ""
        if _is_legacy_mppt_unique_id(unique_id) or _is_legacy_renamed_unique_id(unique_id):
            registry.async_remove(entity_entry.entity_id)
            removed += 1
    if removed:
        _LOGGER.info("Removed %s legacy entities", removed)


async def async_setup_entry(hass: HomeAssistant, entry: HubConfigEntry) -> bool:
    """Set up Fronius Modbus from a config entry."""

    name = entry.data[CONF_NAME]
    host = entry.data[CONF_HOST]
    port = entry.data[CONF_PORT]
    inverter_unit_id = entry.data.get(CONF_INVERTER_UNIT_ID, 1)
    scan_interval = entry.data[CONF_SCAN_INTERVAL]

    meter_unit_id = entry.data[CONF_METER_UNIT_ID]
    if meter_unit_id and meter_unit_id > 0:
        meter_unit_ids = [meter_unit_id]
    else:
        meter_unit_ids = []

    _LOGGER.debug("Setup %s.%s", DOMAIN, name)

    await _async_remove_legacy_mppt_entities(hass, entry)

    entry.runtime_data = hub.Hub(hass = hass, name = name, host = host, port = port, inverter_unit_id=inverter_unit_id, meter_unit_ids=meter_unit_ids, scan_interval = scan_interval)
    
    await entry.runtime_data.init_data(config_entry=entry)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok and getattr(entry, "runtime_data", None) is not None:
        entry.runtime_data.close()

    return unload_ok
