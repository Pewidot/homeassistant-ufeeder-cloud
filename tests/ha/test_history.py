"""Read-only history vectors and privacy boundaries; all HTTP requests mocked."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from custom_components.ufeeder.api import ApiError
from custom_components.ufeeder.history import async_fetch_history

NOW = datetime(2026, 10, 25, 15, tzinfo=ZoneInfo("Europe/Berlin"))
DEVICE = SimpleNamespace(device_id="example", product_key="example-product")


def record(start=NOW - timedelta(hours=1), **changes):
    return {
        "deviceId": DEVICE.device_id,
        "productKey": DEVICE.product_key,
        "startTime": int(start.timestamp() * 1000),
        "endTime": int((start + timedelta(minutes=5)).timestamp() * 1000),
        "plateId": 2,
        "taskStatus": 1,
        "taskSource": 2,
        "userId": "PRIVATE",
        "downLoadURL": "https://private.invalid/signed",
        "platePictureResourceId": "PRIVATE",
        "events": [{"operatorNickName": "PRIVATE"}],
        **changes,
    }


def api(records, count=3, **page_fields):
    return SimpleNamespace(
        async_request=AsyncMock(
            side_effect=[{"records": records, **page_fields}, {"feedingCount": count}]
        )
    )


async def test_exact_get_contract_and_local_midnight_across_dst():
    fake = api([record()])
    result = await async_fetch_history(fake, DEVICE, now=NOW)
    calls = fake.async_request.await_args_list
    assert [call.args for call in calls] == [
        ("GET", "/feeder/v1/feeding-records"),
        ("GET", "/feeder/v1/feeding-records/today"),
    ]
    assert calls[0].kwargs["params"]["pageNum"] == 1
    assert calls[0].kwargs["params"]["pageSize"] == 20
    midnight = datetime(2026, 10, 25, tzinfo=ZoneInfo("Europe/Berlin"))
    assert calls[1].kwargs["params"]["startTime"] == int(midnight.timestamp() * 1000)
    assert NOW.utcoffset() != midnight.utcoffset()
    assert result["feed_count_today"] == 3
    assert result["last_feeding"].tzinfo is timezone.utc


async def test_sanitized_latest_record_and_recovered_enum():
    fake = api([record(NOW - timedelta(hours=3)), record(taskStatus=2, taskSource=1)])
    result = await async_fetch_history(fake, DEVICE, now=NOW)
    assert result["last_status"] == "interrupted"
    assert result["last_source"] == "app_request"
    assert result["last_compartment"] == 2
    assert result["last_end"] - result["last_feeding"] == timedelta(minutes=5)
    assert set(result["recent_records"][0]) == {
        "start",
        "end",
        "compartment",
        "status",
        "source",
    }
    for forbidden in (
        "PRIVATE",
        "https://",
        "deviceId",
        "productKey",
        "events",
        "userId",
    ):
        assert forbidden not in repr(result)


async def test_page_is_bounded_and_not_mislabeled_complete():
    fake = api(
        [record(NOW - timedelta(minutes=i + 5)) for i in range(25)],
        totalRow=35,
        totalPage=2,
    )
    result = await async_fetch_history(fake, DEVICE, now=NOW)
    assert len(result["recent_records"]) == 20
    assert result["history_truncated"]


async def test_invalid_cross_device_and_out_of_window_records_are_ignored():
    fake = api(
        [
            None,
            record(deviceId="another"),
            record(productKey="another"),
            record(startTime=True),
            record(startTime=10**100),
            record(NOW + timedelta(days=1)),
            record(NOW - timedelta(days=31)),
        ]
    )
    result = await async_fetch_history(fake, DEVICE, now=NOW)
    assert result["last_feeding"] is None
    assert result["recent_records"] == []


async def test_unknown_values_are_not_treated_as_success():
    result = await async_fetch_history(
        api([record(taskStatus=999, taskSource=True, plateId=0, endTime=0)]),
        DEVICE,
        now=NOW,
    )
    assert result["last_status"] == "unknown"
    assert result["last_source"] == "unknown"
    assert result["last_compartment"] is None
    assert result["last_end"] is None


@pytest.mark.parametrize("count", [None, True, -1, "3"])
async def test_bad_count_is_not_silently_reported_as_zero(count):
    with pytest.raises(ApiError):
        await async_fetch_history(api([], count), DEVICE, now=NOW)


async def test_malformed_page_and_naive_date_fail_without_extra_requests():
    fake = SimpleNamespace(async_request=AsyncMock(return_value={"records": "wrong"}))
    with pytest.raises(ApiError):
        await async_fetch_history(fake, DEVICE, now=NOW)
    assert fake.async_request.await_count == 1
    with pytest.raises(ValueError):
        await async_fetch_history(fake, DEVICE, now=NOW.replace(tzinfo=None))
    assert fake.async_request.await_count == 1
