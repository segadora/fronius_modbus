from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import socket
from functools import lru_cache
from typing import Any
from urllib.parse import urlparse

import requests
from requests.auth import AuthBase
from requests.utils import parse_dict_header

from .const import FIXED_API_USERNAME

_LOGGER = logging.getLogger(__name__)

MASTER_RTUIF = {"master": {"rtuif": [{"if": "rtu0"}, {"if": "rtu1"}]}}


class ClientIpResolutionError(RuntimeError):
    """Raised when the local IP for Modbus restriction cannot be resolved."""


class FroniusWebAuthError(RuntimeError):
    """Raised when Fronius Web API authentication fails."""


def _as_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _is_enabled(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "on", "yes", "enabled"}
    return bool(value)


def _base_url(host_or_url: str) -> str:
    if "://" in host_or_url:
        parsed = urlparse(host_or_url)
        return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    return f"http://{host_or_url}".rstrip("/")


def _digest_challenge(value: str) -> dict[str, str]:
    if not value:
        return {}
    return parse_dict_header(re.sub(r"^Digest\s+", "", value, flags=re.IGNORECASE))


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


@lru_cache(maxsize=None)
def _hash_mode(base_url: str, user: str, timeout: float) -> str:
    try:
        response = requests.get(f"{base_url}/api/status/common", timeout=timeout)
        response.raise_for_status()
        version = (
            response.json()
            .get("authenticationOptions", {})
            .get("digest", {})
            .get(f"{user}HashingVersion")
        )
    except (requests.RequestException, ValueError):
        version = None
    return "md5" if version == 1 else "sha256"


class XHeaderDigestAuth(AuthBase):
    def __init__(
        self,
        username: str,
        password: str = "",
        token: dict[str, str] | None = None,
        timeout: float = 4.0,
    ) -> None:
        self.username = username
        self.password = password
        self.timeout = timeout
        self.base_url: str | None = None
        self.token = token if isinstance(token, dict) else None
        self.mode: str | None = None
        self.last_nonce = ""
        self.nonce_count = 0
        self.saved_token: dict[str, str] | None = None

    def __call__(self, request):
        if self.base_url is None:
            self.base_url = _base_url(request.url)
        if self.mode is None:
            self.mode = _hash_mode(self.base_url, self.username, self.timeout)
        request.register_hook("response", self.handle_401)
        return request

    def handle_401(self, response: requests.Response, **kwargs: object) -> requests.Response:
        if response.status_code != 401 or "Authorization" in response.request.headers:
            return response

        challenge = _digest_challenge(
            response.headers.get("X-WWW-Authenticate")
            or response.headers.get("www-authenticate", "")
        )
        if "realm" not in challenge or "nonce" not in challenge:
            return response

        response.content
        response.close()

        prepared = response.request.copy()
        prepared.headers["Authorization"] = self._build_header(
            prepared.method,
            self._digest_uri(prepared.url),
            challenge,
        )
        retried = response.connection.send(prepared, **kwargs)
        retried.history.append(response)
        retried.request = prepared
        if retried.status_code != 401 and self.password:
            self.saved_token = {
                "realm": challenge["realm"],
                "token": self._secret(challenge["realm"]),
            }
        return retried

    def _digest_uri(self, url: str) -> str:
        parsed = urlparse(url)
        if parsed.path == "/api/commands/Login":
            return parsed.path
        return parsed.path + (f"?{parsed.query}" if parsed.query else "")

    def _secret(self, realm: str) -> str:
        if not self.password and self.token and self.token.get("realm") == realm:
            return str(self.token["token"])
        payload = f"{self.username}:{realm}:{self.password}".encode()
        if self.mode == "md5":
            return hashlib.md5(payload).hexdigest()
        return hashlib.sha256(payload).hexdigest()

    def _build_header(self, method: str, uri: str, challenge: dict[str, str]) -> str:
        nonce = challenge["nonce"]
        qop = challenge["qop"].split(",")[0]
        if nonce == self.last_nonce:
            self.nonce_count += 1
        else:
            self.nonce_count = 1
        self.last_nonce = nonce

        nc = f"{self.nonce_count:08x}"
        cnonce = os.urandom(8).hex()
        ha2 = hashlib.sha256(f"{method.upper()}:{uri}".encode()).hexdigest()
        digest = hashlib.sha256(
            f"{self._secret(challenge['realm'])}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode()
        ).hexdigest()

        parts = [
            f'username="{self.username}"',
            f'realm="{challenge["realm"]}"',
            f'nonce="{nonce}"',
            f'uri="{uri}"',
            f'response="{digest}"',
            f"qop={qop}",
            f"nc={nc}",
            f'cnonce="{cnonce}"',
        ]
        if challenge.get("opaque"):
            parts.append(f'opaque="{challenge["opaque"]}"')
        return "Digest " + ", ".join(parts)


def _login_response(
    host: str,
    user: str,
    password: str = "",
    token: dict[str, str] | None = None,
    timeout: float = 4.0,
) -> tuple[requests.Response, XHeaderDigestAuth]:
    auth = XHeaderDigestAuth(user, password=password, token=token, timeout=timeout)
    response = requests.get(
        f"http://{host}/api/commands/Login",
        params={"user": user},
        auth=auth,
        timeout=timeout,
    )
    return response, auth


def login(
    host: str,
    user: str,
    password: str = "",
    token: dict[str, str] | None = None,
    timeout: float = 4.0,
) -> bool:
    response, _ = _login_response(host, user, password=password, token=token, timeout=timeout)
    return response.status_code == 200


def mint_token(host: str, user: str, password: str, timeout: float = 4.0) -> dict[str, str] | None:
    response, auth = _login_response(host, user, password=password, timeout=timeout)
    if response.status_code != 200:
        return None
    return auth.saved_token


class FroniusWebClient:
    """Authenticated client for the Fronius web API."""

    def __init__(
        self,
        host: str,
        username: str | None = None,
        password: str = "",
        token: dict[str, str] | None = None,
        timeout: float = 4.0,
    ) -> None:
        self._host = host
        self._username = FIXED_API_USERNAME
        self._password = password
        self._timeout = timeout
        self._auth = XHeaderDigestAuth(self._username, password=password, token=token, timeout=self._timeout)

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
        if response.status_code in (401, 403):
            raise FroniusWebAuthError(f"Fronius Web API auth failed with status {response.status_code}")
        response.raise_for_status()
        return response

    def issued_token(self) -> dict[str, str] | None:
        return self._auth.saved_token

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
        return login(
            self._host,
            self._username,
            password=self._password,
            token=self._auth.token,
            timeout=self._timeout,
        )

    def get_modbus_config(self) -> dict[str, Any]:
        return self._request("get", "/api/config/modbus").json()

    def get_storage_info(self) -> dict[str, str | None]:
        try:
            data = self._request("get", "/api/components/BatteryManagementSystem/readable").json()
            nodes = ((data.get("Body") or {}).get("Data") or {})
            device = next(iter(nodes.values()), {}) if isinstance(nodes, dict) else {}
            attributes = device.get("attributes") if isinstance(device, dict) else {}
            return _parse_storage_info(attributes)
        except FroniusWebAuthError:
            raise
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

    def set_battery_config(
        self,
        mode: int,
        power: int | None = None,
        soc_min: int | None = None,
    ) -> bool:
        soc_mode = "manual" if int(mode) == 1 else "auto"
        payload: dict[str, Any] = {
            "HYB_EM_MODE": mode,
            "BAT_M0_SOC_MODE": soc_mode,
        }
        if int(mode) == 1 and soc_min is not None:
            payload["BAT_M0_SOC_MIN"] = int(soc_min)
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
