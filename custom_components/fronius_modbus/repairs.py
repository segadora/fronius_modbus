from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import RepairsFlow

from .const import MIGRATION_RECONFIGURE_ISSUE_ID_PREFIX
from .flow_common import TokenFlowMixin, async_update_entry_from_input, entry_defaults


class FroniusReconfigureRepairFlow(TokenFlowMixin, RepairsFlow):
    """Repair flow that reuses the reconfigure fields and validation."""

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id
        self._pending_flow_state = None

    async def _async_finish_repair(
        self,
        settings,
        info,
        previous_host,
    ):
        del info
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            return self.async_abort(reason="entry_not_found")

        await async_update_entry_from_input(
            self.hass,
            entry,
            settings,
            previous_host=previous_host,
        )
        return self.async_create_entry(title="", data={})

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            return self.async_abort(reason="entry_not_found")

        defaults = entry_defaults(entry)
        return await self._async_handle_settings_step(
            user_input=user_input,
            step_id="init",
            password_step_id="password",
            defaults=defaults,
            previous_host=defaults["host"],
            on_success=self._async_finish_repair,
        )

    async def async_step_password(self, user_input: dict[str, Any] | None = None):
        return await self._async_handle_password_step(
            user_input=user_input,
            step_id="password",
            restart_step=self.async_step_init,
            on_success=self._async_finish_repair,
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
