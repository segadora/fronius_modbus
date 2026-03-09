from __future__ import annotations

import json
import logging
from typing import Any

import requests
from requests.auth import HTTPDigestAuth

from .const import FIXED_API_USERNAME

_LOGGER = logging.getLogger(__name__)

MASTER_RTUIF = {"master": {"rtuif": [{"if": "rtu0"}, {"if": "rtu1"}]}}


def _as_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _is_enabled(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "on", "yes", "enabled"}
    return bool(value)


class XHeaderDigestAuth(HTTPDigestAuth):
    """Digest auth variant used by Fronius, which returns X-WWW-Authenticate."""

    def handle_401(self, r: requests.Response, **kwargs: Any) -> requests.Response:
        if "www-authenticate" not in r.headers and "X-WWW-Authenticate" in r.headers:
            r.headers["www-authenticate"] = r.headers["X-WWW-Authenticate"]
        return super().handle_401(r, **kwargs)


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    cleaned = str(value).strip()
    return cleaned or None


def _parse_json_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


class FroniusWebClient:
    """Minimal authenticated client for the Fronius web API."""

    def __init__(self, host: str, username: str, password: str, timeout: float = 4.0) -> None:
        self._host = host
        self._username = FIXED_API_USERNAME
        self._timeout = timeout
        self._auth = XHeaderDigestAuth(self._username, password)

    def _url(self, path: str) -> str:
        return f"http://{self._host}{path}"

    def _request(self, method: str, path: str, payload: dict | None = None) -> requests.Response:
        response = requests.request(
            method,
            self._url(path),
            auth=self._auth,
            json=payload,
            timeout=self._timeout,
        )
        response.raise_for_status()
        return response

    def login(self) -> bool:
        response = requests.get(
            self._url(f"/api/commands/Login?user={self._username}"),
            auth=self._auth,
            timeout=self._timeout,
        )
        return response.status_code == 200

    def get_modbus_config(self) -> dict[str, Any]:
        return self._request("get", "/api/config/modbus").json()

    def get_storage_info(self) -> dict[str, str | None]:
        info: dict[str, str | None] = {
            "manufacturer": None,
            "model": "Battery Storage",
            "serial": None,
        }

        try:
            data = self._request("get", "/api/components/BatteryManagementSystem/readable").json()
            body = data.get("Body") or {}
            nodes = body.get("Data") or {}
            if not isinstance(nodes, dict) or not nodes:
                return info

            device = next(iter(nodes.values()))
            if not isinstance(device, dict):
                return info

            attributes = device.get("attributes") or {}
            if not isinstance(attributes, dict):
                return info

            nameplate = _parse_json_object(attributes.get("nameplate"))
            manufacturer = _clean_text(nameplate.get("manufacturer")) or _clean_text(attributes.get("manufacturer"))
            model = (
                _clean_text(attributes.get("model"))
                or _clean_text(nameplate.get("model"))
                or _clean_text(attributes.get("DisplayName"))
            )
            serial = _clean_text(attributes.get("serial")) or _clean_text(nameplate.get("serial"))

            if manufacturer is not None:
                info["manufacturer"] = manufacturer
            if model is not None:
                info["model"] = model
            if serial is not None:
                info["serial"] = serial
        except Exception as err:
            _LOGGER.warning("Failed reading storage identity via web API from %s: %s", self._host, err)
        return info

    def ensure_modbus_enabled(
        self,
        port: int,
        meter_address: int,
        inverter_unit_id: int,
    ) -> bool:
        current = self.get_modbus_config()
        slave = current.get("slave") or {}
        ctr = slave.get("ctr") or {}

        if (
            slave.get("mode") == "tcp"
            and _is_enabled(ctr.get("on"))
            and _as_int(slave.get("port"), port) == int(port)
            and _as_int(slave.get("meterAddress"), meter_address) == int(meter_address)
            and _as_int(slave.get("rtu_inverter_slave_id"), inverter_unit_id) == int(inverter_unit_id)
        ):
            return False

        payload = {
            **MASTER_RTUIF,
            "slave": {
                "rtuif": [],
                "mode": "tcp",
                "port": port,
                "sunspecMode": "int",
                "meterAddress": meter_address,
                "rtu_inverter_slave_id": inverter_unit_id,
                "ctr": {"on": True, "restriction": {"on": False}},
            },
        }
        self._request("post", "/api/config/modbus", payload=payload)
        _LOGGER.info(
            "Enabled Modbus TCP via web API on %s (port=%s inverter_id=%s meter_id=%s)",
            self._host,
            port,
            inverter_unit_id,
            meter_address,
        )
        return True

    def get_battery_config(self) -> dict[str, Any]:
        return self._request("get", "/api/config/batteries").json()

    def set_battery_config(self, mode: int, power: int | None = None) -> bool:
        payload: dict[str, Any] = {
            "HYB_EM_MODE": mode,
            "BAT_M0_SOC_MODE": "manual",
        }
        if power is not None:
            payload["HYB_EM_POWER"] = power
        response = self._request("post", "/api/config/batteries", payload=payload)
        return response.ok

    def set_battery_soc_config(
        self,
        soc_min: int = 6,
        soc_max: int = 99,
        backup_reserved: int = 5,
    ) -> bool:
        payload: dict[str, Any] = {
            "BAT_M0_SOC_MIN": soc_min,
            "BAT_M0_SOC_MODE": "manual",
            "BAT_M0_SOC_MAX": soc_max,
            "HYB_BACKUP_RESERVED": backup_reserved,
        }
        response = self._request("post", "/api/config/batteries", payload=payload)
        return response.ok
