"""Migration helpers for Fronius Modbus."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from . import hub
from .const import (
    API_USERNAME,
    CONF_API_PASSWORD,
    CONF_AUTO_ENABLE_MODBUS,
    CONF_RECONFIGURE_REQUIRED,
    CONF_RESTRICT_MODBUS_TO_THIS_IP,
    DEFAULT_AUTO_ENABLE_MODBUS,
    DEFAULT_RESTRICT_MODBUS_TO_THIS_IP,
    DOMAIN,
    MIGRATION_RECONFIGURE_ISSUE_ID_PREFIX,
)
from .froniuswebclient import mint_token
from .token_store import async_get_token_store

_LOGGER = logging.getLogger(__name__)

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

REPLACED_STORAGE_TELEMETRY_KEYS = (
    "storage_power",
    "storage_charging_dc_current",
    "storage_charging_dc_voltage",
    "storage_charging_dc_power",
    "storage_charging_lifetime_energy",
    "storage_discharging_dc_current",
    "storage_discharging_dc_voltage",
    "storage_discharging_dc_power",
    "storage_discharging_lifetime_energy",
)


def _entry_value(entry: ConfigEntry, key: str, default=None):
    return entry.options.get(key, entry.data.get(key, default))


def _entry_has_key(entry: ConfigEntry, key: str) -> bool:
    return key in entry.options or key in entry.data


def _migration_issue_id(entry: ConfigEntry) -> str:
    return f"{MIGRATION_RECONFIGURE_ISSUE_ID_PREFIX}{entry.entry_id}"


def _legacy_modbus_only_entry(entry: ConfigEntry) -> bool:
    return any(
        not _entry_has_key(entry, key)
        for key in (CONF_AUTO_ENABLE_MODBUS, CONF_RESTRICT_MODBUS_TO_THIS_IP)
    )


def _is_legacy_mppt_unique_id(unique_id: str) -> bool:
    return any(unique_id.endswith(f"_{key}") for key in LEGACY_MPPT_ENTITY_KEYS)


def _is_legacy_renamed_unique_id(unique_id: str) -> bool:
    return any(unique_id.endswith(f"_{key}") for key in LEGACY_RENAMED_ENTITY_KEYS)


def _is_legacy_replaced_web_api_sensor_unique_id(unique_id: str) -> bool:
    return any(unique_id.endswith(f"_{key}") for key in LEGACY_REPLACED_WEB_API_SENSOR_KEYS)


def _is_replaced_storage_telemetry_unique_id(unique_id: str) -> bool:
    return any(unique_id.endswith(f"_{key}") for key in REPLACED_STORAGE_TELEMETRY_KEYS)


def _parse_current_mppt_unique_id(
    unique_id: str,
    entity_prefix: str,
) -> int | None:
    prefix = f"{entity_prefix}_mppt_module_"
    if not unique_id.startswith(prefix):
        return None

    module_idx, separator, _metric = unique_id[len(prefix):].partition("_")
    if separator == "" or not module_idx.isdigit():
        return None
    return int(module_idx) + 1


async def _async_update_entry_auth_state(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    reconfigure_required: bool | None = None,
) -> None:
    new_data = dict(entry.data)
    new_options = dict(entry.options)
    changed = False

    if new_data.pop(CONF_API_PASSWORD, None) is not None:
        changed = True
    if new_options.pop(CONF_API_PASSWORD, None) is not None:
        changed = True

    if reconfigure_required is not None:
        if new_data.get(CONF_RECONFIGURE_REQUIRED) != reconfigure_required:
            new_data[CONF_RECONFIGURE_REQUIRED] = reconfigure_required
            changed = True
        if new_options.get(CONF_RECONFIGURE_REQUIRED) != reconfigure_required:
            new_options[CONF_RECONFIGURE_REQUIRED] = reconfigure_required
            changed = True

    if changed:
        hass.config_entries.async_update_entry(entry, data=new_data, options=new_options)


async def async_sync_reconfigure_issue(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    has_token: bool,
) -> None:
    issue_id = _migration_issue_id(entry)
    needs_reconfigure = bool(_entry_value(entry, CONF_RECONFIGURE_REQUIRED, False)) or not has_token
    if needs_reconfigure:
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


async def async_prepare_entry_token(
    hass: HomeAssistant,
    entry: ConfigEntry,
    host: str,
) -> dict[str, str] | None:
    token_store = async_get_token_store(hass)
    token = await token_store.async_load_token(host, API_USERNAME)
    saved_password = str(_entry_value(entry, CONF_API_PASSWORD, "") or "").strip()

    if token is None and saved_password:
        try:
            token = await hass.async_add_executor_job(
                mint_token,
                host,
                API_USERNAME,
                saved_password,
            )
        except Exception as err:
            _LOGGER.warning("Failed migrating saved Fronius password to token for %s: %s", host, err)
        if token:
            await token_store.async_save_token(
                host,
                realm=token["realm"],
                token=token["token"],
                user=API_USERNAME,
            )

    await _async_update_entry_auth_state(
        hass,
        entry,
        reconfigure_required=not bool(token),
    )
    return token


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

    if entry.version == 1 and entry.minor_version < 3:
        new_data = {**entry.data}
        new_data.setdefault(CONF_AUTO_ENABLE_MODBUS, DEFAULT_AUTO_ENABLE_MODBUS)
        new_data.setdefault(CONF_RESTRICT_MODBUS_TO_THIS_IP, DEFAULT_RESTRICT_MODBUS_TO_THIS_IP)
        if CONF_RECONFIGURE_REQUIRED not in new_data and CONF_RECONFIGURE_REQUIRED not in entry.options:
            new_data[CONF_RECONFIGURE_REQUIRED] = _legacy_modbus_only_entry(entry)

        hass.config_entries.async_update_entry(
            entry,
            data=new_data,
            version=1,
            minor_version=3,
        )

    host = _entry_value(entry, CONF_HOST, "")
    token = await async_prepare_entry_token(hass, entry, host) if host else None
    await async_sync_reconfigure_issue(hass, entry, has_token=token is not None)
    return True


async def async_remove_legacy_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    registry = er.async_get(hass)
    removed = 0
    for entity_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        unique_id = entity_entry.unique_id or ""
        if (
            _is_legacy_mppt_unique_id(unique_id)
            or _is_legacy_renamed_unique_id(unique_id)
            or _is_legacy_replaced_web_api_sensor_unique_id(unique_id)
            or _is_replaced_storage_telemetry_unique_id(unique_id)
        ):
            registry.async_remove(entity_entry.entity_id)
            removed += 1
    if removed:
        _LOGGER.info("Removed %s legacy/replaced entities", removed)


async def async_remove_unused_mppt_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    runtime_data: hub.Hub,
) -> None:
    registry = er.async_get(hass)
    data = runtime_data.data if isinstance(runtime_data.data, dict) else {}
    visible_module_ids = data.get("mppt_visible_module_ids")
    if runtime_data._client.mppt_configured and (
        not isinstance(visible_module_ids, list)
        or not all(isinstance(module_id, int) and module_id > 0 for module_id in visible_module_ids)
    ):
        _LOGGER.debug(
            "Skipping MPPT entity cleanup for %s because visible module ids are unavailable",
            entry.entry_id,
        )
        return

    visible_modules = set(visible_module_ids or [])
    removed = 0
    for entity_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        unique_id = entity_entry.unique_id or ""
        module_id = _parse_current_mppt_unique_id(unique_id, runtime_data.entity_prefix)
        if module_id is None:
            continue
        if not runtime_data._client.mppt_configured or module_id not in visible_modules:
            registry.async_remove(entity_entry.entity_id)
            removed += 1

    if removed:
        _LOGGER.info("Removed %s unused MPPT entities", removed)
