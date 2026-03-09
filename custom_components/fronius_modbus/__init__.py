"""The Fronius Modbus integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import Platform
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from homeassistant.const import CONF_NAME, CONF_HOST, CONF_PORT, CONF_SCAN_INTERVAL
from .const import (
    DOMAIN,
    CONF_INVERTER_UNIT_ID,
    CONF_METER_UNIT_ID,
    CONF_API_USERNAME,
    CONF_API_PASSWORD,
    CONF_AUTO_ENABLE_MODBUS,
    CONF_RESTRICT_MODBUS_TO_THIS_IP,
    CONF_RECONFIGURE_REQUIRED,
    MIGRATION_RECONFIGURE_ISSUE_ID_PREFIX,
    DEFAULT_AUTO_ENABLE_MODBUS,
    DEFAULT_RESTRICT_MODBUS_TO_THIS_IP,
    DEFAULT_NAME,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_INVERTER_UNIT_ID,
    FIXED_API_USERNAME,
)

from . import hub

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SELECT, Platform.SWITCH, Platform.NUMBER, Platform.SENSOR]

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
    "minimum_reserve",
    "api_soc_max",
)

LEGACY_REPLACED_WEB_API_SENSOR_KEYS = (
    "api_charge_from_ac",
    "api_charge_from_grid",
)

def _is_legacy_mppt_unique_id(unique_id: str) -> bool:
    return any(unique_id.endswith(f"_{key}") for key in LEGACY_MPPT_ENTITY_KEYS)


def _is_legacy_renamed_unique_id(unique_id: str) -> bool:
    return any(unique_id.endswith(f"_{key}") for key in LEGACY_RENAMED_ENTITY_KEYS)


def _is_legacy_replaced_web_api_sensor_unique_id(unique_id: str) -> bool:
    return any(unique_id.endswith(f"_{key}") for key in LEGACY_REPLACED_WEB_API_SENSOR_KEYS)


async def _async_remove_legacy_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    registry = er.async_get(hass)
    removed = 0
    for entity_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        unique_id = entity_entry.unique_id or ""
        if (
            _is_legacy_mppt_unique_id(unique_id)
            or _is_legacy_renamed_unique_id(unique_id)
            or _is_legacy_replaced_web_api_sensor_unique_id(unique_id)
        ):
            registry.async_remove(entity_entry.entity_id)
            removed += 1
    if removed:
        _LOGGER.info("Removed %s legacy entities", removed)


def _entry_value(entry: ConfigEntry, key: str, default=None):
    return entry.options.get(key, entry.data.get(key, default))


def _migration_issue_id(entry: ConfigEntry) -> str:
    return f"{MIGRATION_RECONFIGURE_ISSUE_ID_PREFIX}{entry.entry_id}"


def _entry_has_key(entry: ConfigEntry, key: str) -> bool:
    return key in entry.options or key in entry.data


def _entry_needs_reconfigure(entry: ConfigEntry) -> bool:
    return bool(_entry_value(entry, CONF_RECONFIGURE_REQUIRED, False)) or not bool(
        _entry_value(entry, CONF_API_PASSWORD, "")
    )


def _legacy_modbus_only_entry(entry: ConfigEntry) -> bool:
    return any(
        not _entry_has_key(entry, key)
        for key in (CONF_API_USERNAME, CONF_API_PASSWORD, CONF_AUTO_ENABLE_MODBUS)
    )


def _sync_reconfigure_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    issue_id = _migration_issue_id(entry)
    if _entry_needs_reconfigure(entry):
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=True,
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="legacy_modbus_only_entry_reconfigure",
            translation_placeholders={
                "entry_title": entry.title or _entry_value(entry, CONF_NAME, "Fronius"),
            },
            data={"entry_id": entry.entry_id},
        )
        return

    ir.async_delete_issue(hass, DOMAIN, issue_id)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old config entries."""
    _LOGGER.debug(
        "Migrating config entry %s version=%s minor=%s",
        entry.entry_id,
        entry.version,
        entry.minor_version,
    )

    if entry.version > 1:
        _LOGGER.error("Unsupported config entry version: %s", entry.version)
        return False

    if entry.version == 1 and entry.minor_version < 2:
        new_data = {**entry.data}
        new_data[CONF_API_USERNAME] = FIXED_API_USERNAME
        new_data.setdefault(CONF_API_PASSWORD, "")
        new_data.setdefault(CONF_AUTO_ENABLE_MODBUS, DEFAULT_AUTO_ENABLE_MODBUS)
        if CONF_RECONFIGURE_REQUIRED not in new_data and CONF_RECONFIGURE_REQUIRED not in entry.options:
            new_data[CONF_RECONFIGURE_REQUIRED] = _legacy_modbus_only_entry(entry)

        hass.config_entries.async_update_entry(
            entry,
            data=new_data,
            version=1,
            minor_version=2,
        )

    _sync_reconfigure_issue(hass, entry)
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: HubConfigEntry) -> bool:
    """Set up Fronius Modbus from a config entry."""

    name = _entry_value(entry, CONF_NAME, DEFAULT_NAME)
    host = _entry_value(entry, CONF_HOST)
    port = _entry_value(entry, CONF_PORT, DEFAULT_PORT)
    inverter_unit_id = _entry_value(entry, CONF_INVERTER_UNIT_ID, DEFAULT_INVERTER_UNIT_ID)
    scan_interval = _entry_value(entry, CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    api_password = _entry_value(entry, CONF_API_PASSWORD)
    api_username = FIXED_API_USERNAME if api_password else None
    auto_enable_modbus = _entry_value(
        entry,
        CONF_AUTO_ENABLE_MODBUS,
        DEFAULT_AUTO_ENABLE_MODBUS,
    )
    restrict_modbus_to_this_ip = _entry_value(
        entry,
        CONF_RESTRICT_MODBUS_TO_THIS_IP,
        DEFAULT_RESTRICT_MODBUS_TO_THIS_IP,
    )

    meter_unit_id = _entry_value(entry, CONF_METER_UNIT_ID)
    if meter_unit_id and meter_unit_id > 0:
        meter_unit_ids = [meter_unit_id]
    else:
        meter_unit_ids = []

    _LOGGER.debug("Setup %s.%s", DOMAIN, name)

    await _async_remove_legacy_entities(hass, entry)
    _sync_reconfigure_issue(hass, entry)

    entry.runtime_data = hub.Hub(
        hass=hass,
        name=name,
        host=host,
        port=port,
        inverter_unit_id=inverter_unit_id,
        meter_unit_ids=meter_unit_ids,
        scan_interval=scan_interval,
        api_username=api_username,
        api_password=api_password,
        auto_enable_modbus=auto_enable_modbus,
        restrict_modbus_to_this_ip=restrict_modbus_to_this_ip,
    )

    await entry.runtime_data.init_data(config_entry=entry)

    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok and getattr(entry, "runtime_data", None) is not None:
        entry.runtime_data.close()

    return unload_ok
