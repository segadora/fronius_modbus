"""The Fronius Modbus integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT, CONF_SCAN_INTERVAL, Platform
from homeassistant.core import HomeAssistant

from . import hub, migrations
from .const import (
    API_USERNAME,
    CONF_INVERTER_UNIT_ID,
    CONF_METER_UNIT_ID,
    CONF_METER_UNIT_IDS,
    CONF_RESTRICT_MODBUS_TO_THIS_IP,
    DEFAULT_INVERTER_UNIT_ID,
    DEFAULT_NAME,
    DEFAULT_METER_UNIT_IDS,
    DEFAULT_PORT,
    DEFAULT_RESTRICT_MODBUS_TO_THIS_IP,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SELECT, Platform.SWITCH, Platform.NUMBER, Platform.SENSOR]

type HubConfigEntry = ConfigEntry[hub.Hub]


def _entry_value(entry: ConfigEntry, key: str, default=None):
    return entry.options.get(key, entry.data.get(key, default))


def _entry_meter_unit_ids(entry: ConfigEntry) -> list[int]:
    meter_unit_ids = _entry_value(entry, CONF_METER_UNIT_IDS)
    if isinstance(meter_unit_ids, list):
        normalized: list[int] = []
        for unit_id in meter_unit_ids:
            try:
                unit_id_int = int(unit_id)
            except (TypeError, ValueError):
                continue
            if unit_id_int > 0:
                normalized.append(unit_id_int)
        return normalized

    meter_unit_id = _entry_value(entry, CONF_METER_UNIT_ID, DEFAULT_METER_UNIT_IDS[0])
    try:
        meter_unit_id = int(meter_unit_id)
    except (TypeError, ValueError):
        meter_unit_id = DEFAULT_METER_UNIT_IDS[0]
    return [meter_unit_id] if meter_unit_id > 0 else []


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old config entries."""
    return await migrations.async_migrate_entry(hass, entry)


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: HubConfigEntry) -> bool:
    """Set up Fronius Modbus from a config entry."""
    name = _entry_value(entry, CONF_NAME, DEFAULT_NAME)
    host = _entry_value(entry, CONF_HOST)
    port = _entry_value(entry, CONF_PORT, DEFAULT_PORT)
    inverter_unit_id = _entry_value(entry, CONF_INVERTER_UNIT_ID, DEFAULT_INVERTER_UNIT_ID)
    scan_interval = _entry_value(entry, CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    restrict_modbus_to_this_ip = _entry_value(
        entry,
        CONF_RESTRICT_MODBUS_TO_THIS_IP,
        DEFAULT_RESTRICT_MODBUS_TO_THIS_IP,
    )

    meter_unit_ids = _entry_meter_unit_ids(entry)

    _LOGGER.debug("Setup %s.%s", DOMAIN, name)

    await migrations.async_remove_legacy_entities(hass, entry)
    api_token = await migrations.async_prepare_entry_token(hass, entry, host)
    await migrations.async_sync_reconfigure_issue(hass, entry, has_token=api_token is not None)

    entry.runtime_data = hub.Hub(
        hass=hass,
        name=name,
        host=host,
        port=port,
        inverter_unit_id=inverter_unit_id,
        meter_unit_ids=meter_unit_ids,
        scan_interval=scan_interval,
        api_username=API_USERNAME if api_token else None,
        api_token=api_token,
        auto_enable_modbus=False,
        restrict_modbus_to_this_ip=restrict_modbus_to_this_ip,
    )

    await entry.runtime_data.init_data(config_entry=entry)
    await migrations.async_remove_unused_mppt_entities(hass, entry, entry.runtime_data)
    await migrations.async_sync_reconfigure_issue(
        hass,
        entry,
        has_token=entry.runtime_data.web_api_configured,
    )

    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok and getattr(entry, "runtime_data", None) is not None:
        entry.runtime_data.close()

    return unload_ok
