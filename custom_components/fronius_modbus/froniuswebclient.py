from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import socket
from typing import Any
from urllib.parse import urlparse

import requests
from requests.auth import HTTPDigestAuth
from requests.utils import parse_dict_header

from .const import FIXED_API_USERNAME

_LOGGER = logging.getLogger(__name__)

MASTER_RTUIF = {"master": {"rtuif": [{"if": "rtu0"}, {"if": "rtu1"}]}}
LEGACY_DIGEST = "legacy_digest"
FRONIUS_DIGEST = "fronius_digest"
_STRATEGY_CACHE: dict[tuple[str, str], tuple[str, str | None]] = {}


def _as_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _is_enabled(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "on", "yes", "enabled"}
    return bool(value)


def _normalize_base_url(host_or_url: str) -> str:
    if "://" in host_or_url:
        parsed = urlparse(host_or_url)
        return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    return f"http://{host_or_url}".rstrip("/")


def _parse_digest_challenge(value: str) -> dict[str, str]:
    if not value:
        return {}
    return parse_dict_header(re.sub(r"^Digest\s+", "", value, flags=re.IGNORECASE))


def _probe_login(
    host_or_url: str,
    user: str,
    timeout: float = 4.0,
    verify: bool | None = None,
) -> tuple[str, str | None]:
    base_url = _normalize_base_url(host_or_url)
    cache_key = (urlparse(base_url).netloc or base_url, user)
    if cache_key in _STRATEGY_CACHE:
        return _STRATEGY_CACHE[cache_key]

    request_options: dict[str, object] = {"timeout": timeout}
    if verify is not None:
        request_options["verify"] = verify

    hash_mode = None
    try:
        response = requests.get(f"{base_url}/api/status/common", **request_options)
        response.raise_for_status()
        version = (
            response.json()
            .get("authenticationOptions", {})
            .get("digest", {})
            .get(f"{user}HashingVersion")
        )
        if version is not None:
            hash_mode = "md5" if version == 1 else "sha256"
    except (requests.RequestException, ValueError):
        pass

    challenge = {}
    try:
        response = requests.get(
            f"{base_url}/api/commands/Login",
            params={"user": user},
            **request_options,
        )
        challenge = _parse_digest_challenge(
            response.headers.get("X-WWW-Authenticate", "")
        )
    except requests.RequestException:
        pass

    qop = challenge.get("qop", "")
    algorithm = challenge.get("algorithm", "")

    if algorithm.upper() == "SHA256" and "auth" in qop.split(","):
        details = (FRONIUS_DIGEST, hash_mode)
    else:
        details = (LEGACY_DIGEST, None)

    _STRATEGY_CACHE[cache_key] = details
    return details


class XHeaderDigestAuth(HTTPDigestAuth):
    """Digest auth variant used by Fronius."""

    def __init__(
        self,
        username: str,
        password: str,
        timeout: float = 4.0,
        verify: bool | None = None,
    ) -> None:
        super().__init__(username, password)
        self.timeout = timeout
        self.verify = verify
        self._strategy: str | None = None
        self._hash_mode: str | None = None
        self._last_nonce = ""
        self._nonce_count = 0

    def __call__(self, request):
        self._ensure_strategy(request.url)
        if self._strategy == LEGACY_DIGEST:
            return super().__call__(request)

        self.init_per_thread_state()
        try:
            self._thread_local.pos = request.body.tell()
        except AttributeError:
            self._thread_local.pos = None
        request.register_hook("response", self.handle_401)
        request.register_hook("response", self.handle_redirect)
        self._thread_local.num_401_calls = 1
        return request

    def handle_401(self, response: requests.Response, **kwargs: Any) -> requests.Response:
        self._copy_authenticate_header(response)
        self._ensure_strategy(response.request.url)
        if self._strategy == LEGACY_DIGEST:
            return super().handle_401(response, **kwargs)
        return self._handle_fronius_401(response, **kwargs)

    def _copy_authenticate_header(self, response: requests.Response) -> None:
        if (
            "www-authenticate" not in response.headers
            and "X-WWW-Authenticate" in response.headers
        ):
            response.headers["www-authenticate"] = response.headers["X-WWW-Authenticate"]

    def _ensure_strategy(self, url: str) -> None:
        if self._strategy is not None:
            return
        self._strategy, self._hash_mode = _probe_login(
            url,
            self.username,
            self.timeout,
            self.verify,
        )

    def _handle_fronius_401(
        self,
        response: requests.Response,
        **kwargs: Any,
    ) -> requests.Response:
        if response.status_code != 401:
            return response
        if self._thread_local.num_401_calls is not None:
            if self._thread_local.num_401_calls >= 2:
                return response
            self._thread_local.num_401_calls += 1
        if self._thread_local.pos is not None:
            response.request.body.seek(self._thread_local.pos)

        response.content
        response.close()

        challenge = _parse_digest_challenge(response.headers.get("www-authenticate", ""))
        if not challenge or "realm" not in challenge or "nonce" not in challenge:
            return response

        request_uri = self._digest_uri(response.request.url)
        prepared = response.request.copy()
        prepared.headers["Authorization"] = self._build_fronius_header(
            prepared.method,
            request_uri,
            challenge,
        )
        retried = response.connection.send(prepared, **kwargs)
        self._copy_authenticate_header(retried)
        retried.history.append(response)
        retried.request = prepared
        return retried

    def _digest_uri(self, url: str) -> str:
        parsed = urlparse(url)
        path = parsed.path or "/"
        if path == "/api/commands/Login":
            return path
        if parsed.query:
            return f"{path}?{parsed.query}"
        return path

    def _hash_secret(self, realm: str) -> str:
        value = f"{self.username}:{realm}:{self.password}".encode()
        if self._hash_mode == "md5":
            return hashlib.md5(value).hexdigest()
        return hashlib.sha256(value).hexdigest()

    def _build_fronius_header(
        self,
        method: str,
        uri: str,
        challenge: dict[str, str],
    ) -> str:
        nonce = challenge["nonce"]
        qop = challenge.get("qop", "").split(",")[0]
        opaque = challenge.get("opaque")

        if nonce == self._last_nonce:
            self._nonce_count += 1
        else:
            self._nonce_count = 1
        self._last_nonce = nonce

        nc_value = f"{self._nonce_count:08x}"
        cnonce = os.urandom(8).hex()
        ha2 = hashlib.sha256(f"{method.upper()}:{uri}".encode()).hexdigest()
        response = hashlib.sha256(
            f"{self._hash_secret(challenge['realm'])}:{nonce}:{nc_value}:{cnonce}:auth:{ha2}".encode()
        ).hexdigest()

        parts = [
            f'username="{self.username}"',
            f'realm="{challenge["realm"]}"',
            f'nonce="{nonce}"',
            f'uri="{uri}"',
            f'response="{response}"',
        ]
        if opaque:
            parts.append(f'opaque="{opaque}"')
        if qop:
            parts.append(f"qop={qop}")
            parts.append(f"nc={nc_value}")
            parts.append(f'cnonce="{cnonce}"')
        return "Digest " + ", ".join(parts)


def login(host: str, user: str, password: str, timeout: float = 4.0) -> bool:
    url = f"http://{host}/api/commands/Login?user={user}"
    response = requests.get(
        url,
        auth=XHeaderDigestAuth(user, password, timeout=timeout),
        timeout=timeout,
    )
    return response.status_code == 200


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
        self._password = password
        self._timeout = timeout
        self._auth = XHeaderDigestAuth(self._username, password, timeout=self._timeout)

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
        return login(self._host, self._username, self._password, self._timeout)

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
