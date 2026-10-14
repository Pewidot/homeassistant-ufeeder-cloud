"""Mocked UCSP contract tests; no cloud account or real device is used."""

import asyncio
import inspect
import time
import traceback
from copy import deepcopy
from functools import partial
from types import SimpleNamespace

import aiohttp
import pytest
from aioresponses import CallbackResult, aioresponses
from yarl import URL

from custom_components.ufeeder.api import (
    ApiError,
    CannotConnect,
    FeederState,
    InvalidAuth,
    InvalidRegion,
    UFeederApi,
    read_tokens,
)
from custom_components.ufeeder.const import BOOTSTRAP, PRODUCT_KEY

DOMAIN = "https://ucsp-eu.ubtrobot.com"
AREAS_URL = f"{BOOTSTRAP}/usercenter/v1/area-domains"
LOGIN_URL = f"{DOMAIN}/usercenter/v1/users/login/password"
REFRESH_URL = f"{DOMAIN}/usercenter/v1/users/refresh-token"
TEST_URL = f"{DOMAIN}/read"
AREA_DATA = {"groupList": [{"areaDomainList": [{"area": "DE", "domainUrl": DOMAIN}]}]}


def token_group(prefix="new"):
    now = time.time()
    return {
        "accessToken": {"token": f"{prefix}-access", "expireAt": now + 3600},
        "refreshToken": {"token": f"{prefix}-refresh", "expireAt": now + 86400},
    }


def saved_session():
    return {**read_tokens(token_group("old")), "domain": DOMAIN, "userId": "123"}


@pytest.fixture
async def api():
    async with aiohttp.ClientSession() as http:
        yield UFeederApi(
            http,
            "person@example.com",
            "test-password",
            "DE",
            "client-123",
            saved_session(),
        )


@pytest.fixture
def mocked(monkeypatch):
    # aioresponses 0.7.9 predates aiohttp 3.14's required constructor argument.
    # Retain the real response implementation and supply only its unused writer.
    if "stream_writer" in inspect.signature(aiohttp.ClientResponse).parameters:
        monkeypatch.setattr(
            "aioresponses.core.ClientResponse",
            partial(
                aiohttp.ClientResponse, stream_writer=SimpleNamespace(output_size=0)
            ),
        )
    with aioresponses() as responses:
        yield responses


def request_count(mocked, method, url):
    return len(mocked.requests.get((method, URL(url)), []))


async def test_login_exact_body_signature_and_nested_token_result(
    api, mocked, monkeypatch
):
    api._session = {}
    api.username = "person@example.com"
    monkeypatch.setattr("custom_components.ufeeder.api.time.time", lambda: 1700000000.0)
    monkeypatch.setattr(
        "custom_components.ufeeder.api.secrets.choice", lambda chars: "A"
    )
    mocked.get(AREAS_URL, payload={"code": 0, "data": AREA_DATA})
    mocked.post(
        LOGIN_URL,
        payload={
            "code": 0,
            "data": {"tokenDTO": token_group(), "userDTO": {"userId": 123}},
        },
    )
    await api.async_login()
    call = mocked.requests[("POST", URL(LOGIN_URL))][0]
    assert call.kwargs["json"] == {
        "account": "person@example.com",
        "accountType": 2,
        "areaCode": None,
        "password": "aed51f9cbf16de20395162fd07030c70",
        "area": "DE",
    }
    headers = call.kwargs["headers"]
    assert headers["X-UBT-AppId"] == "25030009"
    assert headers["X-UBT-ClientId"] == "client-123"
    assert (
        headers["X-UBT-Sign"]
        == "cb1e5489244c5eb57547549647346d00 1700000000000 AAAAAAAAAA v2"
    )
    assert "Authorization" not in headers
    assert call.kwargs["allow_redirects"] is False
    assert api.user_id == "123"
    assert api.domain == DOMAIN
    assert api.export_session()["token"] == "new-access"
    assert api.export_session()["refreshToken"] == "new-refresh"


@pytest.mark.parametrize("code", [20009, 20010, 20021])
async def test_login_account_errors_are_auth_failures(api, mocked, code):
    mocked.get(AREAS_URL, payload={"code": 0, "data": AREA_DATA})
    mocked.post(LOGIN_URL, payload={"code": code, "msg": "private vendor details"})
    with pytest.raises(InvalidAuth) as error:
        await api.async_login()
    assert "private" not in str(error.value)
    assert api.export_session()["token"] == "old-access"


async def test_login_rejects_unrecognized_domain_before_sending_password(api, mocked):
    mocked.get(
        AREAS_URL,
        payload={
            "code": 0,
            "data": {
                "groupList": [
                    {
                        "areaDomainList": [
                            {"area": "DE", "domainUrl": "https://other.invalid"}
                        ]
                    }
                ],
            },
        },
    )
    with pytest.raises(InvalidRegion):
        await api.async_login()
    assert len(mocked.requests) == 1


async def test_login_cannot_replace_a_different_account(api, mocked):
    before = api.export_session()
    mocked.get(AREAS_URL, payload={"code": 0, "data": AREA_DATA})
    mocked.post(
        LOGIN_URL,
        payload={
            "code": 0,
            "data": {"tokenDTO": token_group(), "userDTO": {"userId": 456}},
        },
    )
    with pytest.raises(InvalidAuth, match="different account"):
        await api.async_login()
    assert api.export_session() == before


@pytest.mark.parametrize("user_id", [True, None, "", "None", "123/other", 0])
async def test_malformed_user_id_cannot_enter_a_mqtt_topic(api, mocked, user_id):
    before = api.export_session()
    mocked.get(AREAS_URL, payload={"code": 0, "data": AREA_DATA})
    mocked.post(
        LOGIN_URL,
        payload={
            "code": 0,
            "data": {"tokenDTO": token_group(), "userDTO": {"userId": user_id}},
        },
    )
    with pytest.raises(ApiError):
        await api.async_login()
    assert api.export_session() == before


async def test_concurrent_expired_requests_refresh_once_and_rotate_both_tokens(
    api, mocked
):
    api._session["expireAt"] = time.time() - 1
    updates = []
    api._session_updated = updates.append

    async def refresh(url, **kwargs):
        await asyncio.sleep(0.01)
        return CallbackResult(payload={"code": 0, "data": token_group()})

    mocked.post(REFRESH_URL, callback=refresh)
    mocked.get(TEST_URL, payload={"code": 0, "data": "ok"}, repeat=True)
    assert (
        await asyncio.gather(*(api.async_request("GET", "read") for _ in range(4)))
        == ["ok"] * 4
    )
    assert request_count(mocked, "POST", REFRESH_URL) == 1
    assert request_count(mocked, "POST", LOGIN_URL) == 0
    refresh_call = mocked.requests[("POST", URL(REFRESH_URL))][0]
    assert refresh_call.kwargs["json"] == {"refreshToken": "old-refresh"}
    assert "Authorization" not in refresh_call.kwargs["headers"]
    for call in mocked.requests[("GET", URL(TEST_URL))]:
        assert call.kwargs["headers"]["Authorization"] == "new-access"
    assert len(updates) == 1
    assert updates[0]["refreshToken"] == "new-refresh"


async def test_get_401_retried_once_with_refreshed_raw_token(api, mocked):
    mocked.get(TEST_URL, status=401)
    mocked.post(REFRESH_URL, payload={"code": 0, "data": token_group()})
    mocked.get(TEST_URL, payload={"code": 0, "data": "ok"})
    assert await api.async_request("GET", "read") == "ok"
    calls = mocked.requests[("GET", URL(TEST_URL))]
    assert [call.kwargs["headers"]["Authorization"] for call in calls] == [
        "old-access",
        "new-access",
    ]
    assert request_count(mocked, "POST", REFRESH_URL) == 1


async def test_second_401_latches_reauth_without_refresh_storm(api, mocked):
    mocked.get(TEST_URL, status=401, repeat=True)
    mocked.post(REFRESH_URL, payload={"code": 0, "data": token_group()})
    with pytest.raises(InvalidAuth):
        await api.async_request("GET", "read")
    with pytest.raises(InvalidAuth, match="Reauthentication"):
        await api.async_request("GET", "read")
    assert request_count(mocked, "GET", TEST_URL) == 2
    assert request_count(mocked, "POST", REFRESH_URL) == 1
    assert request_count(mocked, "POST", LOGIN_URL) == 0


async def test_post_401_never_replayed(api, mocked):
    mocked.post(TEST_URL, status=401)
    with pytest.raises(InvalidAuth):
        await api.async_request("POST", "read", body={"operation": "test"})
    assert request_count(mocked, "POST", TEST_URL) == 1
    assert request_count(mocked, "POST", REFRESH_URL) == 0


async def test_shared_device_forbidden_does_not_refresh_or_log_in(api, mocked):
    mocked.get(TEST_URL, status=403)
    with pytest.raises(ApiError) as error:
        await api.async_request("GET", "read")
    assert error.value.code == 403
    assert request_count(mocked, "GET", TEST_URL) == 1
    assert request_count(mocked, "POST", REFRESH_URL) == 0
    assert not api._auth_failed


async def test_rejected_refresh_requires_explicit_reauth_without_password_login(
    api, mocked
):
    api._session["expireAt"] = time.time() - 1
    before = api.export_session()
    mocked.post(REFRESH_URL, payload={"code": 20012, "msg": "private details"})
    for _ in range(2):
        with pytest.raises(InvalidAuth):
            await api.async_request("GET", "read")
    assert request_count(mocked, "POST", REFRESH_URL) == 1
    assert request_count(mocked, "POST", LOGIN_URL) == 0
    assert api.export_session() == before


async def test_expired_refresh_never_attempts_password_login(api, mocked):
    api._session.update(expireAt=0, refreshExpireAt=0)
    with pytest.raises(InvalidAuth):
        await api.async_request("GET", "read")
    assert not mocked.requests


async def test_temporary_refresh_failure_can_recover_without_password_login(
    api, mocked
):
    api._session["expireAt"] = time.time() - 1
    mocked.post(REFRESH_URL, status=503)
    with pytest.raises(CannotConnect):
        await api.async_request("GET", "read")
    assert not api._auth_failed
    mocked.post(REFRESH_URL, payload={"code": 0, "data": token_group()})
    mocked.get(TEST_URL, payload={"code": 0, "data": "ok"})
    assert await api.async_request("GET", "read") == "ok"
    assert request_count(mocked, "POST", LOGIN_URL) == 0


@pytest.mark.parametrize(
    "expiry", [True, None, "not-a-date", float("nan"), float("inf"), -1, 0]
)
def test_invalid_token_expiry_rejected_atomically(expiry):
    group = token_group()
    group["refreshToken"]["expireAt"] = expiry
    with pytest.raises(ApiError):
        read_tokens(group)


async def test_malformed_refresh_keeps_previous_complete_token_pair(api, mocked):
    api._session["expireAt"] = time.time() - 1
    before = api.export_session()
    mocked.post(
        REFRESH_URL,
        payload={"code": 0, "data": {"accessToken": token_group()["accessToken"]}},
    )
    with pytest.raises(ApiError):
        await api.async_request("GET", "read")
    assert api.export_session() == before


@pytest.mark.parametrize("payload", [[], None, {}, {"code": False}, {"code": "0"}])
async def test_malformed_response_is_protocol_error(api, mocked, payload):
    mocked.get(TEST_URL, payload=payload)
    with pytest.raises(ApiError):
        await api.async_request("GET", "read")


async def test_exception_traceback_does_not_include_network_secret_text(api, mocked):
    mocked.get(
        TEST_URL, exception=aiohttp.ClientConnectionError("private-access-token")
    )
    with pytest.raises(CannotConnect) as error:
        await api.async_request("GET", "read")
    assert "private-access-token" not in "".join(
        traceback.format_exception(error.value)
    )


async def test_device_discovery_shared_owner_same_product_and_deduplication(
    api, mocked
):
    url = f"{DOMAIN}/platform/v1/device-relation/user/devices?productKey={PRODUCT_KEY}"
    mocked.get(
        url,
        payload={
            "code": 0,
            "data": [
                {
                    "deviceId": "one",
                    "productKey": PRODUCT_KEY,
                    "deviceName": "Kitchen",
                    "relationType": 1,
                },
                {
                    "deviceId": "two",
                    "productKey": PRODUCT_KEY,
                    "deviceName": "Shared",
                    "relationType": 2,
                },
                {
                    "deviceId": "one",
                    "productKey": PRODUCT_KEY,
                    "deviceName": "Duplicate",
                },
                {"deviceId": "other", "productKey": "other"},
            ],
        },
    )
    assert [
        (device.device_id, device.name) for device in await api.async_devices()
    ] == [("one", "Kitchen"), ("two", "Shared")]


@pytest.mark.parametrize("device_id", [None, "", True, "a/other", "#"])
async def test_malformed_device_id_rejected(api, mocked, device_id):
    url = f"{DOMAIN}/platform/v1/device-relation/user/devices?productKey={PRODUCT_KEY}"
    mocked.get(
        url,
        payload={
            "code": 0,
            "data": [{"deviceId": device_id, "productKey": PRODUCT_KEY}],
        },
    )
    with pytest.raises(ApiError):
        await api.async_devices()


async def test_shadow_allowlist_type_validation_timestamps_and_snapshot_replacement(
    api, mocked
):
    device = FeederState(
        "device", "Feeder", properties={"10:11:12": 99}, timestamps={"10:11:12": 1}
    )
    online_url = f"{DOMAIN}/device-manager/v1/device/onoffline?deviceIds=device&productKey={PRODUCT_KEY}"
    shadow_url = f"{DOMAIN}/device-shadow/v1/device/shadow?deviceId=device&productKey={PRODUCT_KEY}"
    mocked.get(
        online_url,
        payload={"code": 0, "data": [{"deviceId": "device", "onoffline": 0}]},
    )
    shadow = {
        "10:11:11": {"value": 24, "time": 123},
        "10:11:9": {"value": False, "time": 124},
        "10:11:8": {"value": "false", "time": 125},
        "10:11:19": {"value": {"invalid": 1}, "time": 126},
        "10:11:7": {
            "value": {"feedDurationSeconds": 180, "heatBeforeFeeding": False},
            "time": True,
        },
        "10:13:2": {"value": "private-factory-secret", "time": 999},
        "10:10:9": {"value": {"ssid": "private-wifi"}, "time": 999},
    }
    before = deepcopy(shadow)
    mocked.get(shadow_url, payload={"code": 0, "data": shadow})
    result = await api.async_device_state(device)
    assert result.online is False
    assert set(result.properties) == {"10:11:11", "10:11:9", "10:11:7"}
    assert result.timestamps == {"10:11:11": 123, "10:11:9": 124}
    assert "private-" not in repr(result)
    assert shadow == before


async def test_missing_online_device_is_unknown_and_malformed_rows_fail(api, mocked):
    url = f"{DOMAIN}/device-manager/v1/device/onoffline?deviceIds=device&productKey={PRODUCT_KEY}"
    mocked.get(url, payload={"code": 0, "data": [None]})
    with pytest.raises(ApiError):
        await api.async_device_state(FeederState("device", "Feeder"))
