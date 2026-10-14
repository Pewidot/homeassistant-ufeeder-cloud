"""Asynchronous UCSP authentication and read API. Never logs response bodies."""

from __future__ import annotations

import asyncio
import hashlib
import math
import secrets
import string
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from .const import (
    ALLOWED_DOMAINS,
    APP_ID,
    APP_KEY,
    BOOTSTRAP,
    PASSWORD_SALT,
    PRODUCT_KEY,
)


class UFeederError(Exception):
    """A safe-to-display cloud error, containing no vendor response text."""


class CannotConnect(UFeederError):
    """Connection, TLS, timeout, or server failure."""


class InvalidAuth(UFeederError):
    """Credentials or session were rejected."""


class InvalidRegion(UFeederError):
    """Account country is not supported by the server."""


class ApiError(UFeederError):
    """Business or protocol error."""

    def __init__(self, code: int | None = None) -> None:
        self.code = code
        super().__init__(
            f"UCSP error code {code}" if code is not None else "Invalid UCSP response"
        )


@dataclass
class FeederState:
    """Only allowlisted device state reaches HA; factory secrets are discarded."""

    device_id: str
    name: str
    product_key: str = PRODUCT_KEY
    online: bool | None = None
    properties: dict[str, Any] = field(default_factory=dict)
    timestamps: dict[str, int] = field(default_factory=dict)
    history: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.product_key}:{self.device_id}"


ALLOWED_PROPERTIES = frozenset(
    [f"10:11:{index}" for index in range(1, 24)]
    + [f"10:10:{index}" for index in (*range(1, 9), 10)]
)
_BOOLEAN_PROPERTIES = frozenset(
    ("10:11:8", "10:11:9", "10:11:10", "10:11:17", "10:11:22", "10:10:5")
)
_STRUCT_PROPERTIES = frozenset(
    ("10:11:7", "10:11:13", "10:11:14", "10:11:15", "10:11:21", "10:10:1", "10:10:2")
)
_ENUM_PROPERTIES = frozenset(
    (
        "10:11:1",
        "10:11:2",
        "10:11:3",
        "10:11:4",
        "10:11:16",
        "10:11:18",
        "10:10:3",
        "10:10:6",
        "10:10:7",
    )
)


def _valid_property(path: str, value: Any) -> bool:
    if path in _BOOLEAN_PROPERTIES:
        return type(value) is bool
    if path in _STRUCT_PROPERTIES:
        return isinstance(value, dict)
    if path in _ENUM_PROPERTIES:
        return type(value) in (str, int)
    return type(value) is int


def password_digest(password: str) -> str:
    return hashlib.md5((password + PASSWORD_SALT).encode("utf-8")).hexdigest()


def read_tokens(group: Any) -> dict[str, Any]:
    """Require a complete token pair before replacing the current session."""
    try:
        access, refresh = group["accessToken"], group["refreshToken"]
        if not all(
            isinstance(item["token"], str) and item["token"]
            for item in (access, refresh)
        ):
            raise ValueError
        expires = []
        for item in (access, refresh):
            if isinstance(item["expireAt"], bool):
                raise TypeError
            expiry = float(item["expireAt"])
            if not math.isfinite(expiry) or expiry <= 0:
                raise ValueError
            expires.append(expiry)
        return {
            "token": access["token"],
            "expireAt": expires[0],
            "refreshToken": refresh["token"],
            "refreshExpireAt": expires[1],
        }
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ApiError from None


class UFeederApi:
    """One stable client ID and one refresh lock per account."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
        region: str,
        client_id: str,
        saved_session: dict[str, Any] | None = None,
        session_updated: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._http = session
        self.username = username.strip()
        self._password = password
        self.region = region.upper()
        self.client_id = client_id
        self._session = dict(saved_session or {})
        self._session_updated = session_updated
        self._auth_lock = asyncio.Lock()
        self._auth_failed = False

    @property
    def domain(self) -> str:
        domain = self._session.get("domain", BOOTSTRAP)
        if not isinstance(domain, str) or domain not in ALLOWED_DOMAINS:
            raise InvalidRegion("Unrecognized UCSP server")
        return domain

    @property
    def user_id(self) -> str:
        return str(self._session.get("userId", ""))

    @property
    def unique_id(self) -> str:
        return f"{self.domain.removeprefix('https://')}:{self.user_id}"

    def export_session(self) -> dict[str, Any]:
        return dict(self._session)

    def _headers(self, auth: bool) -> dict[str, str]:
        ts = str(int(time.time() * 1000))
        nonce = "".join(
            secrets.choice(string.ascii_letters + string.digits) for _ in range(10)
        )
        signature = hashlib.md5(
            (ts + APP_KEY + nonce + self.client_id).encode()
        ).hexdigest()
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-UBT-AppId": APP_ID,
            "X-UBT-ClientId": self.client_id,
            "X-UBT-Language": "en",
            "X-UBT-Sign": f"{signature} {ts} {nonce} v2",
        }
        if auth:
            headers["Authorization"] = self._session["token"]
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        auth: bool = True,
        params: dict | None = None,
        body: dict | None = None,
        domain: str | None = None,
    ) -> Any:
        try:
            async with self._http.request(
                method,
                f"{domain or self.domain}/{path.lstrip('/')}",
                headers=self._headers(auth),
                params=params,
                json=body,
                timeout=aiohttp.ClientTimeout(total=20),
                allow_redirects=False,
            ) as response:
                if response.status == 401:
                    raise InvalidAuth("Cloud authentication rejected")
                if response.status >= 500 or response.status == 429:
                    raise CannotConnect(f"Cloud HTTP {response.status}")
                if response.status != 200:
                    raise ApiError(response.status)
                try:
                    payload = await response.json()
                except (ValueError, aiohttp.ContentTypeError) as err:
                    raise ApiError from err
        except (aiohttp.ClientError, TimeoutError):
            # Do not include exception text: it may contain URLs or credentials.
            raise CannotConnect("Cannot reach the U Feeder cloud") from None
        if not isinstance(payload, dict) or type(payload.get("code")) is not int:
            raise ApiError
        code = payload["code"]
        if code in (20010, 20012):
            raise InvalidAuth(f"Cloud authentication rejected ({code})")
        if code != 0:
            raise ApiError(code)
        return payload.get("data")

    def _save_session(self, data: dict[str, Any]) -> None:
        self._session.update(data)
        if self._session_updated is not None:
            self._session_updated(self.export_session())

    async def _login(self) -> None:
        if "@" not in self.username or not self._password:
            raise InvalidAuth("An email account and password are required")
        areas = await self._request(
            "GET", "usercenter/v1/area-domains", auth=False, domain=BOOTSTRAP
        )
        try:
            domain = next(
                area["domainUrl"]
                for group in areas["groupList"]
                for area in group["areaDomainList"]
                if area["area"] == self.region
            )
        except (KeyError, TypeError, StopIteration) as err:
            raise InvalidRegion("Choose the account country used in the app") from err
        if not isinstance(domain, str) or domain not in ALLOWED_DOMAINS:
            raise InvalidRegion("Unrecognized UCSP server")
        try:
            data = await self._request(
                "POST",
                "usercenter/v1/users/login/password",
                auth=False,
                domain=domain,
                body={
                    "account": self.username,
                    "accountType": 2,
                    "areaCode": None,
                    "password": password_digest(self._password),
                    "area": self.region,
                },
            )
        except ApiError as err:
            # Shipped UserCenterServiceImpl login error mapping at 0x81f7b4.
            if err.code in (20009, 20021):
                raise InvalidAuth(f"Account rejected ({err.code})") from None
            raise
        try:
            tokens = read_tokens(data["tokenDTO"])
            user_id = data["userDTO"]["userId"]
            if (
                type(user_id) not in (str, int)
                or not str(user_id).isascii()
                or not str(user_id).isdecimal()
                or int(user_id) <= 0
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError) as err:
            raise ApiError from err
        if self.user_id and (self.user_id != str(user_id) or self.domain != domain):
            raise InvalidAuth("The cloud returned a different account")
        self._save_session({**tokens, "domain": domain, "userId": str(user_id)})

    async def async_login(self) -> None:
        async with self._auth_lock:
            await self._login()
            self._auth_failed = False

    async def async_ensure_token(self, *, rejected_token: str | None = None) -> None:
        async with self._auth_lock:
            if self._auth_failed:
                raise InvalidAuth("Reauthentication required")
            try:
                access_expiry = float(self._session.get("expireAt", 0))
                refresh_expiry = float(self._session.get("refreshExpireAt", 0))
                if not math.isfinite(access_expiry) or not math.isfinite(
                    refresh_expiry
                ):
                    raise ValueError
            except (ValueError, TypeError):
                self._auth_failed = True
                raise InvalidAuth("Saved session is invalid; sign in again") from None
            if (
                self._session.get("token")
                and access_expiry > time.time() + 60
                and (rejected_token is None or rejected_token != self._session["token"])
            ):
                return
            try:
                if self._session.get("refreshToken") and refresh_expiry > time.time():
                    try:
                        group = await self._request(
                            "POST",
                            "usercenter/v1/users/refresh-token",
                            auth=False,
                            body={"refreshToken": self._session["refreshToken"]},
                        )
                    except InvalidAuth:
                        raise InvalidAuth(
                            "Session ended; sign in again in the integration settings"
                        ) from None
                    self._save_session(read_tokens(group))
                    return
                # UCSP may invalidate another session on password login. Only an
                # explicit config/reauth flow may log in; never reclaim a session.
                raise InvalidAuth(
                    "Session expired; sign in again in the integration settings"
                )
            except InvalidAuth:
                self._auth_failed = True
                raise

    async def async_request(
        self,
        method: str,
        path: str,
        *,
        auth: bool = True,
        params: dict | None = None,
        body: dict | None = None,
    ) -> Any:
        method = method.upper()
        if auth:
            await self.async_ensure_token()
        token = self._session.get("token")
        try:
            return await self._request(
                method, path, auth=auth, params=params, body=body
            )
        except InvalidAuth:
            # Only reads are replayed. Commands must never be duplicated.
            if not auth or method != "GET":
                raise
            await self.async_ensure_token(rejected_token=token)
            retry_token = self._session.get("token")
            try:
                return await self._request(
                    method, path, auth=True, params=params, body=body
                )
            except InvalidAuth:
                async with self._auth_lock:
                    if self._session.get("token") == retry_token:
                        self._auth_failed = True
                raise

    async def async_devices(self) -> list[FeederState]:
        data = await self.async_request(
            "GET",
            "platform/v1/device-relation/user/devices",
            params={"productKey": PRODUCT_KEY},
        )
        if not isinstance(data, list):
            raise ApiError
        result = []
        seen = set()
        for item in data:
            if not isinstance(item, dict):
                raise ApiError
            if item.get("productKey") != PRODUCT_KEY:
                continue
            device_id = item.get("deviceId")
            if (
                not isinstance(device_id, str)
                or not device_id
                or any(c in device_id for c in "/+#\x00")
            ):
                raise ApiError
            name = item.get("deviceName")
            if name is not None and not isinstance(name, str):
                raise ApiError
            if device_id not in seen:
                result.append(FeederState(device_id, name or "U Feeder", PRODUCT_KEY))
                seen.add(device_id)
        return result

    async def async_device_state(self, device: FeederState) -> FeederState:
        params = {"deviceId": device.device_id, "productKey": device.product_key}
        online = await self.async_request(
            "GET",
            "device-manager/v1/device/onoffline",
            params={"deviceIds": device.device_id, "productKey": device.product_key},
        )
        if not isinstance(online, list) or any(
            not isinstance(item, dict) for item in online
        ):
            raise ApiError
        device.online = next(
            (
                bool(item["onoffline"])
                for item in online
                if item.get("deviceId") == device.device_id
                and type(item.get("onoffline")) is int
                and item["onoffline"] in (0, 1)
            ),
            None,
        )
        shadow = await self.async_request(
            "GET", "device-shadow/v1/device/shadow", params=params
        )
        if not isinstance(shadow, dict):
            raise ApiError
        # A fresh snapshot replaces stale values, including properties removed by
        # firmware. The coordinator separately merges newer MQTT observations.
        device.properties.clear()
        device.timestamps.clear()
        for key, record in shadow.items():
            if (
                key in ALLOWED_PROPERTIES
                and isinstance(record, dict)
                and "value" in record
                and _valid_property(key, record["value"])
            ):
                device.properties[key] = deepcopy(record["value"])
                if type(record.get("time")) is int and record["time"] >= 0:
                    device.timestamps[key] = record["time"]
        return device
