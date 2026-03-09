from __future__ import annotations

import json
import logging
import socket
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


class ClientIpResolutionError(RuntimeError):
    """Raised when the local IP for Modbus restriction cannot be resolved."""


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


def _parse_storage_info(attributes: Any) -> dict[str, str | None]:
    if not isinstance(attributes, dict):
        return {"manufacturer": None, "model": "Battery Storage", "serial": None}

    nameplate = _parse_json_object(attributes.get("nameplate"))
    return {
        "manufacturer": _clean_text(nameplate.get("manufacturer")) or _clean_text(attributes.get("manufacturer")),
        "model": (
            _clean_text(attributes.get("model"))
            or _clean_text(nameplate.get("model"))
            or _clean_text(attributes.get("DisplayName"))
            or "Battery Storage"
        ),
        "serial": _clean_text(attributes.get("serial")) or _clean_text(nameplate.get("serial")),
    }


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

    def _resolve_client_ip(self) -> str:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect((self._host, 80))
                client_ip = sock.getsockname()[0]
        except OSError as err:
            raise ClientIpResolutionError(f"Failed resolving local IP for {self._host}") from err

        if not client_ip or client_ip.startswith("127."):
            raise ClientIpResolutionError(f"Invalid local IP resolved for {self._host}: {client_ip!r}")
        return client_ip

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
        try:
            data = self._request("get", "/api/components/BatteryManagementSystem/readable").json()
            nodes = ((data.get("Body") or {}).get("Data") or {})
            device = next(iter(nodes.values()), {}) if isinstance(nodes, dict) else {}
            attributes = device.get("attributes") if isinstance(device, dict) else {}
            return _parse_storage_info(attributes)
        except Exception as err:
            _LOGGER.warning("Failed reading storage identity via web API from %s: %s", self._host, err)
        return _parse_storage_info(None)

    def ensure_modbus_enabled(
        self,
        port: int,
        meter_address: int,
        inverter_unit_id: int,
        restrict_to_client_ip: bool = False,
    ) -> bool:
        current = self.get_modbus_config()
        slave = current.get("slave") or {}
        ctr = slave.get("ctr") or {}
        current_restriction = ctr.get("restriction") or {}
        restriction_on = bool(restrict_to_client_ip)
        restriction_ip = self._resolve_client_ip() if restriction_on else None

        if (
            slave.get("mode") == "tcp"
            and _is_enabled(ctr.get("on"))
            and _as_int(slave.get("port"), port) == int(port)
            and _as_int(slave.get("meterAddress"), meter_address) == int(meter_address)
            and _as_int(slave.get("rtu_inverter_slave_id"), inverter_unit_id) == int(inverter_unit_id)
            and _is_enabled(current_restriction.get("on")) == restriction_on
            and (not restriction_on or current_restriction.get("ip") == restriction_ip)
        ):
            return False

        restriction_payload: dict[str, Any] = {"on": restriction_on}
        if restriction_ip:
            restriction_payload["ip"] = restriction_ip

        payload = {
            **MASTER_RTUIF,
            "slave": {
                "rtuif": [],
                "mode": "tcp",
                "port": port,
                "sunspecMode": "int",
                "meterAddress": meter_address,
                "rtu_inverter_slave_id": inverter_unit_id,
                "ctr": {"on": True, "restriction": restriction_payload},
            },
        }
        self._request("post", "/api/config/modbus", payload=payload)
        _LOGGER.info(
            "Enabled Modbus TCP via web API on %s (port=%s inverter_id=%s meter_id=%s restriction=%s ip=%s)",
            self._host,
            port,
            inverter_unit_id,
            meter_address,
            restriction_on,
            restriction_ip,
        )
        return True

    def get_battery_config(self) -> dict[str, Any]:
        return self._request("get", "/api/config/batteries").json()

    def set_battery_config(self, mode: int, power: int | None = None) -> bool:
        soc_mode = "manual" if int(mode) == 1 else "auto"
        payload: dict[str, Any] = {
            "HYB_EM_MODE": mode,
            "BAT_M0_SOC_MODE": soc_mode,
        }
        if int(mode) != 1:
            payload["BAT_M0_SOC_MIN"] = 5
            payload["BAT_M0_SOC_MAX"] = 100
        if power is not None:
            payload["HYB_EM_POWER"] = power
        return self._request("post", "/api/config/batteries", payload=payload).ok

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
        return self._request("post", "/api/config/batteries", payload=payload).ok

    def set_battery_charge_sources(self, charge_from_grid: bool, charge_from_ac: bool) -> bool:
        payload = {
            "HYB_EVU_CHARGEFROMGRID": bool(charge_from_grid),
            "HYB_BM_CHARGEFROMAC": bool(charge_from_ac),
        }
        return self._request("post", "/api/config/batteries", payload=payload).ok
