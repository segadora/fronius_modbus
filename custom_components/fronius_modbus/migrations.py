"""Migration helpers for Fronius Modbus.

This file only preserves the upgrade path from commit
32cd901e5590d97aa4f77af52b4df5a7745d2bbd to the current integration layout.
"""

from __future__ import annotations

import logging
import re

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from . import hub
from .const import (
    API_USERNAME,
    CONF_METER_UNIT_ID,
    CONF_METER_UNIT_IDS,
    CONF_RECONFIGURE_REQUIRED,
    DOMAIN,
    MIGRATION_RECONFIGURE_ISSUE_ID_PREFIX,
)
from .token_store import async_get_token_store

_LOGGER = logging.getLogger(__name__)

_TARGET_VERSION = 1
_TARGET_MINOR_VERSION = 8

_LEGACY_METER_ENTITY_RE = re.compile(r".*_m\d+_.+")
_LEGACY_METER_DEVICE_RE = re.compile(r".*_meter\d+")
_LEGACY_ENTITY_SUFFIXES = (
    "export_limit_rate",
    "export_limit_enable",
    "minimum_reserve",
    "storage_power",
)


def _entry_value(entry: ConfigEntry, key: str, default=None):
    return entry.options.get(key, entry.data.get(key, default))


def _migration_issue_id(entry: ConfigEntry) -> str:
    return f"{MIGRATION_RECONFIGURE_ISSUE_ID_PREFIX}{entry.entry_id}"


def _legacy_entity_needs_removal(unique_id: str) -> bool:
    return bool(_LEGACY_METER_ENTITY_RE.fullmatch(unique_id)) or any(
        unique_id.endswith(f"_{suffix}") for suffix in _LEGACY_ENTITY_SUFFIXES
    )


def _legacy_meter_device_needs_removal(device) -> bool:
    identifiers = getattr(device, "identifiers", set())
    return any(
        identifier_domain == DOMAIN and _LEGACY_METER_DEVICE_RE.fullmatch(identifier)
        for identifier_domain, identifier in identifiers
    )


def _parse_current_mppt_unique_id(unique_id: str, entity_prefix: str) -> int | None:
    prefix = f"{entity_prefix}_mppt_module_"
    if not unique_id.startswith(prefix):
        return None

    module_idx, separator, _metric = unique_id[len(prefix):].partition("_")
    if separator == "" or not module_idx.isdigit():
        return None
    return int(module_idx) + 1


async def _async_set_reconfigure_required(
    hass: HomeAssistant,
    entry: ConfigEntry,
    required: bool,
) -> None:
    new_data = dict(entry.data)
    new_options = dict(entry.options)
    changed = False

    if new_data.get(CONF_RECONFIGURE_REQUIRED) != required:
        new_data[CONF_RECONFIGURE_REQUIRED] = required
        changed = True
    if new_options.get(CONF_RECONFIGURE_REQUIRED) != required:
        new_options[CONF_RECONFIGURE_REQUIRED] = required
        changed = True

    if changed:
        hass.config_entries.async_update_entry(entry, data=new_data, options=new_options)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old config entries from 32cd901 to the current shape."""
    _LOGGER.debug(
        "Migrating config entry %s version=%s minor=%s",
        entry.entry_id,
        entry.version,
        entry.minor_version,
    )

    if entry.version > _TARGET_VERSION:
        _LOGGER.error("Unsupported config entry version: %s", entry.version)
        return False

    if entry.version == _TARGET_VERSION and entry.minor_version < _TARGET_MINOR_VERSION:
        new_data = dict(entry.data)
        new_options = dict(entry.options)

        new_data.pop(CONF_METER_UNIT_ID, None)
        new_data.pop(CONF_METER_UNIT_IDS, None)
        new_options.pop(CONF_METER_UNIT_ID, None)
        new_options.pop(CONF_METER_UNIT_IDS, None)
        new_data[CONF_RECONFIGURE_REQUIRED] = True
        new_options[CONF_RECONFIGURE_REQUIRED] = True

        hass.config_entries.async_update_entry(
            entry,
            data=new_data,
            options=new_options,
            version=_TARGET_VERSION,
            minor_version=_TARGET_MINOR_VERSION,
        )

    return True


async def async_prepare_entry_token(
    hass: HomeAssistant,
    entry: ConfigEntry,
    host: str,
) -> dict[str, str] | None:
    token = await async_get_token_store(hass).async_load_token(host, API_USERNAME)
    await _async_set_reconfigure_required(hass, entry, not bool(token))
    return token


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


async def async_remove_legacy_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    """Remove entity ids that only existed on 32cd901."""
    registry = er.async_get(hass)
    device_registry = dr.async_get(hass)

    removed_entities = 0
    for entity_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        unique_id = entity_entry.unique_id or ""
        if _legacy_entity_needs_removal(unique_id):
            registry.async_remove(entity_entry.entity_id)
            removed_entities += 1

    removed_devices = 0
    for device in dr.async_entries_for_config_entry(device_registry, entry.entry_id):
        if _legacy_meter_device_needs_removal(device):
            device_registry.async_remove_device(device.id)
            removed_devices += 1

    if removed_entities:
        _LOGGER.info("Removed %s legacy entities from pre-web-api config", removed_entities)
    if removed_devices:
        _LOGGER.info("Removed %s legacy meter devices from pre-web-api config", removed_devices)


async def async_remove_unused_mppt_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    runtime_data: hub.Hub,
) -> None:
    """Remove MPPT entities that are now intentionally hidden."""
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
