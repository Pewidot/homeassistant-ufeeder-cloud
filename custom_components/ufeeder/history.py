"""Bounded read-only feeding history, without account or image metadata."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone

from homeassistant.util import dt as dt_util

from .api import ApiError

HISTORY_DAYS = 30
MAX_RECORDS = 20
STATUS_NAMES = (
    "restart",
    "end",
    "interrupted",
    "fault",
    "power_supply_battery",
    "preparing",
    "starting",
    "door_open",
    "pet_detected",
    "tray_aligning",
    "door_fault",
    "tray_fault",
    "timeout",
)
SOURCE_NAMES = ("physical_button", "app_request", "schedule", "auto_feed")


def _enum(value, names):
    return names[value] if type(value) is int and 0 <= value < len(names) else "unknown"


def _timestamp(value) -> datetime | None:
    if type(value) is not int or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def _record(raw, device, since, now) -> dict | None:
    if not isinstance(raw, Mapping):
        return None
    if raw.get("deviceId", device.device_id) != device.device_id:
        return None
    if raw.get("productKey", device.product_key) != device.product_key:
        return None
    start = _timestamp(raw.get("startTime"))
    if start is None or not since <= start <= now:
        return None
    end = _timestamp(raw.get("endTime"))
    if end is not None and not start <= end <= now:
        end = None
    compartment = raw.get("plateId")
    if type(compartment) is not int or not 1 <= compartment <= 4:
        compartment = None
    return {
        "start": start.isoformat(),
        "end": end.isoformat() if end else None,
        "compartment": compartment,
        "status": _enum(raw.get("taskStatus"), STATUS_NAMES),
        "source": _enum(raw.get("taskSource"), SOURCE_NAMES),
    }


async def async_fetch_history(api, device, *, now: datetime | None = None) -> dict:
    """Read one 20-record page and today's count, using HA's configured timezone.

    The result describes the newest record returned within a 30-day window,
    not an unbounded export. The caller controls its polling interval. No raw
    event data, identifiers, names, image resources or URLs leave this helper.
    """
    now = now if now is not None else dt_util.now()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("History queries require a timezone-aware time")
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    since = now - timedelta(days=HISTORY_DAYS)
    common = {
        "productKey": device.product_key,
        "deviceId": device.device_id,
        "endTime": int(now.timestamp() * 1000),
    }
    page = await api.async_request(
        "GET",
        "/feeder/v1/feeding-records",
        params={
            **common,
            "startTime": int(since.timestamp() * 1000),
            "pageNum": 1,
            "pageSize": MAX_RECORDS,
        },
    )
    if not isinstance(page, Mapping) or not isinstance(page.get("records"), list):
        raise ApiError()
    count = await api.async_request(
        "GET",
        "/feeder/v1/feeding-records/today",
        params={
            **common,
            "startTime": int(today.timestamp() * 1000),
        },
    )
    if not isinstance(count, Mapping) or type(count.get("feedingCount")) is not int:
        raise ApiError()
    if not 0 <= count["feedingCount"] <= 0xFFFFFFFF:
        raise ApiError()
    records = [
        clean
        for raw in page["records"][:MAX_RECORDS]
        if (clean := _record(raw, device, since, now)) is not None
    ]
    records.sort(key=lambda item: item["start"], reverse=True)
    latest = records[0] if records else {}
    total = page.get("totalRow")
    total_pages = page.get("totalPage")
    truncated = (
        len(page["records"]) > MAX_RECORDS
        or (type(total) is int and total > len(page["records"]))
        or (type(total_pages) is int and total_pages > 1)
    )
    return {
        "last_feeding": datetime.fromisoformat(latest["start"]) if latest else None,
        "last_end": datetime.fromisoformat(latest["end"])
        if latest.get("end")
        else None,
        "last_compartment": latest.get("compartment"),
        "last_status": latest.get("status"),
        "last_source": latest.get("source"),
        "feed_count_today": count["feedingCount"],
        "recent_records": records,
        "history_truncated": truncated,
        "history_since": since.astimezone(timezone.utc),
        "fetched_at": now.astimezone(timezone.utc),
    }
