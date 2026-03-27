from __future__ import annotations

import logging
import re

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import slugify

from . import hub
from .const import (
    API_USERNAME,
    CONF_METER_UNIT_ID,
    CONF_METER_UNIT_IDS,
    CONF_RECONFIGURE_REQUIRED,
    DOMAIN,
    MIGRATION_RECONFIGURE_ISSUE_ID_PREFIX,
    SINGLE_PHASE_UNSUPPORTED_METER_SENSOR_KEYS,
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
    "tempcab",
)
_FALLBACK_STORAGE_SOC_MINIMUM_ENTITY_ID_RE = re.compile(
    r"^number\.battery_storage_soc_minimum(?:_\d+)?$"
)
_FALLBACK_STORAGE_MODEL = "Battery Storage"


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


def _is_fallback_storage_soc_minimum_entity_id(entity_id: str) -> bool:
    return bool(_FALLBACK_STORAGE_SOC_MINIMUM_ENTITY_ID_RE.fullmatch(entity_id))


def _single_phase_meter_phase_count(runtime_data: hub.Hub, unit_id: int) -> int | None:
    try:
        phase_count = runtime_data.data.get(f"meter_{int(unit_id)}_phase_count")
        return int(phase_count) if phase_count is not None else None
    except (TypeError, ValueError):
        return None


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


async def async_remove_unsupported_single_phase_meter_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    runtime_data: hub.Hub,
) -> None:
    single_phase_unit_ids = [
        int(unit_id)
        for unit_id in runtime_data._client._meter_unit_ids
        if _single_phase_meter_phase_count(runtime_data, unit_id) == 1
    ]
    if not single_phase_unit_ids:
        return

    unsupported_unique_ids = {
        f"{runtime_data.entity_prefix}_meter_{unit_id}_{key}"
        for unit_id in single_phase_unit_ids
        for key in SINGLE_PHASE_UNSUPPORTED_METER_SENSOR_KEYS
    }
    registry = er.async_get(hass)
    removed = 0
    for entity_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        unique_id = entity_entry.unique_id or ""
        if unique_id in unsupported_unique_ids:
            registry.async_remove(entity_entry.entity_id)
            removed += 1

    if removed:
        _LOGGER.info(
            "Removed %s unsupported single-phase meter entities for %s",
            removed,
            entry.title or entry.entry_id,
        )


async def async_repair_soc_minimum_entity_id(
    hass: HomeAssistant,
    entry: ConfigEntry,
    runtime_data: hub.Hub,
) -> None:
    """Repair the SoC Minimum entity id after storage metadata becomes available."""
    if not runtime_data.storage_configured or not runtime_data.web_api_configured:
        return

    storage_model = str(runtime_data.data.get("s_model") or "").strip()
    if not storage_model or storage_model == _FALLBACK_STORAGE_MODEL:
        return

    registry = er.async_get(hass)
    soc_minimum_unique_id = f"{runtime_data.entity_prefix}_soc_minimum"
    entity_entry = next(
        (
            candidate
            for candidate in er.async_entries_for_config_entry(registry, entry.entry_id)
            if candidate.domain == "number" and candidate.unique_id == soc_minimum_unique_id
        ),
        None,
    )
    if entity_entry is None:
        return

    if not _is_fallback_storage_soc_minimum_entity_id(entity_entry.entity_id):
        return

    if entity_entry.device_id is None:
        _LOGGER.debug(
            "Skipping SoC Minimum entity-id repair for %s because the entity has no device id",
            entry.entry_id,
        )
        return

    device_registry = dr.async_get(hass)
    device_entry = device_registry.async_get(entity_entry.device_id)
    if device_entry is None:
        _LOGGER.debug(
            "Skipping SoC Minimum entity-id repair for %s because the storage device is missing",
            entry.entry_id,
        )
        return

    device_name = str(device_entry.name_by_user or device_entry.name or "").strip()
    if not device_name:
        _LOGGER.debug(
            "Skipping SoC Minimum entity-id repair for %s because the storage device has no name",
            entry.entry_id,
        )
        return

    name_suffix = slugify(
        getattr(entity_entry, "object_id_base", None)
        or getattr(entity_entry, "original_name", None)
        or "soc_minimum"
    )
    preferred_entity_id = f"number.{slugify(device_name)}_{name_suffix}"
    regenerated_entity_id = registry.async_regenerate_entity_id(entity_entry)

    if regenerated_entity_id == entity_entry.entity_id:
        return

    if regenerated_entity_id != preferred_entity_id:
        _LOGGER.debug(
            "Skipping SoC Minimum entity-id repair for %s because %s would collide or does not match preferred target %s",
            entry.entry_id,
            regenerated_entity_id,
            preferred_entity_id,
        )
        return

    registry.async_update_entity(
        entity_entry.entity_id,
        new_entity_id=regenerated_entity_id,
    )
    _LOGGER.info(
        "Repaired SoC Minimum entity id for %s: %s -> %s",
        entry.entry_id,
        entity_entry.entity_id,
        regenerated_entity_id,
    )
