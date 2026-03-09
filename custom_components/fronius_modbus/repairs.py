from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.repairs import RepairsFlow
from homeassistant.data_entry_flow import FlowResult

from .config_flow import (
    _build_schema,
    _expand_user_input,
    async_update_entry_from_input,
    validate_input,
    AddressesNotUnique,
    CannotConnect,
    CannotResolveLocalIp,
    InvalidApiCredentials,
    InvalidHost,
    InvalidPort,
    MissingApiPassword,
    ScanIntervalTooShort,
    UnsupportedHardware,
)
from .const import MIGRATION_RECONFIGURE_ISSUE_ID_PREFIX

_LOGGER = logging.getLogger(__name__)


def _set_form_error(errors: dict[str, str], err: Exception) -> None:
    if isinstance(err, CannotConnect):
        errors["base"] = "cannot_connect"
    elif isinstance(err, InvalidPort):
        errors["base"] = "invalid_port"
    elif isinstance(err, InvalidHost):
        errors["host"] = "invalid_host"
    elif isinstance(err, ScanIntervalTooShort):
        errors["base"] = "scan_interval_too_short"
    elif isinstance(err, MissingApiPassword):
        errors["base"] = "missing_api_password"
    elif isinstance(err, InvalidApiCredentials):
        errors["base"] = "invalid_api_credentials"
    elif isinstance(err, CannotResolveLocalIp):
        errors["base"] = "cannot_resolve_local_ip"
    elif isinstance(err, UnsupportedHardware):
        errors["base"] = "unsupported_hardware"
    elif isinstance(err, AddressesNotUnique):
        errors["base"] = "modbus_address_conflict"
    else:
        _LOGGER.exception("Unexpected exception in repair flow")
        errors["base"] = "unknown"


class FroniusReconfigureRepairFlow(RepairsFlow):
    """Repair flow that reuses the reconfigure fields and validation."""

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            return self.async_abort(reason="entry_not_found")

        errors: dict[str, str] = {}
        defaults = _expand_user_input({}, {**entry.data, **entry.options})

        if user_input is not None:
            try:
                validated_input = _expand_user_input(user_input, defaults)
                await validate_input(self.hass, validated_input)
                await async_update_entry_from_input(self.hass, entry, validated_input)
                return self.async_create_entry(title="", data={})
            except Exception as err:  # pylint: disable=broad-except
                _set_form_error(errors, err)

        return self.async_show_form(
            step_id="init",
            data_schema=_build_schema(defaults),
            errors=errors,
        )


async def async_create_fix_flow(
    hass,
    issue_id: str,
    data: dict[str, Any] | None,
) -> RepairsFlow:
    """Create fix flow for a Fronius repairs issue."""
    if not issue_id.startswith(MIGRATION_RECONFIGURE_ISSUE_ID_PREFIX):
        raise ValueError(f"Unknown issue: {issue_id}")

    entry_id = str((data or {}).get("entry_id") or issue_id.removeprefix(MIGRATION_RECONFIGURE_ISSUE_ID_PREFIX))
    return FroniusReconfigureRepairFlow(entry_id)
