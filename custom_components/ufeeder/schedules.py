"""Pure codecs and calendar helpers for the recovered U Feeder schedule contract.

Times and weekday masks on the device are UTC. Bit zero means Sunday. There is
no date or timezone field.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
from typing import Any

MAX_SCHEDULES = 10
MIN_START_SEPARATION_MINUTES = 60
ALLOWED_DURATION_MINUTES = (*range(3, 31), *range(60, 721, 60))
ADD_SCHEDULES_PATH = (10, 11, 7)
REMOVE_SCHEDULES_PATH = (10, 11, 8)
UPDATE_SCHEDULES_PATH = (10, 11, 9)
SCHEDULES_PROPERTY = "10:11:13"


@dataclass(frozen=True)
class FeedingSchedule:
    """A device schedule; tray_compartment is one based, time is UTC.

    id=0 is the app's add-request value. Existing IDs are assigned by the device.
    Optional FeedOptions fields are omitted by the app's schedule editor.
    Preserve them when editing a schedule reported by another client.
    """

    id: int = 0
    hour: int = 0
    minute: int = 0
    weekdays: int = 127
    enabled: bool = True
    tray_compartment: int = 1
    duration_seconds: int = 180
    heat_before_feeding: bool | None = None
    heating_temperature: int | None = None
    operator: int | None = None
    unknown_fields: bytes = field(default=b"", repr=False)
    unknown_start_fields: bytes = field(default=b"", repr=False)
    unknown_option_fields: bytes = field(default=b"", repr=False)

    def as_dict(self) -> dict[str, Any]:
        """Return named protobuf JSON without credential-bearing fields."""
        options: dict[str, Any] = {
            "trayCompartmentIndex": self.tray_compartment,
            "feedDurationSeconds": self.duration_seconds,
        }
        for key, value in (
            ("heatBeforeFeeding", self.heat_before_feeding),
            ("expectedHeatingTemperature", self.heating_temperature),
            ("operator", self.operator),
        ):
            if value is not None:
                options[key] = value
        return {
            "id": self.id,
            "start": {"hour": self.hour, "minute": self.minute},
            "enabled": self.enabled,
            "weekdays": self.weekdays,
            "feedOptions": options,
        }


def _integer(value: Any, name: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer in {low}..{high}")
    return value


def _validate_wire(schedule: FeedingSchedule) -> None:
    for name in ("id", "weekdays", "tray_compartment", "duration_seconds"):
        _integer(getattr(schedule, name), name, 0, 0xFFFFFFFF)
    _integer(schedule.hour, "hour", 0, 23)
    _integer(schedule.minute, "minute", 0, 59)
    if type(schedule.enabled) is not bool:
        raise ValueError("enabled must be a boolean")
    if (
        schedule.heat_before_feeding is not None
        and type(schedule.heat_before_feeding) is not bool
    ):
        raise ValueError("heat_before_feeding must be a boolean")
    if schedule.heating_temperature is not None:
        _integer(
            schedule.heating_temperature,
            "heating_temperature",
            -(1 << 31),
            (1 << 31) - 1,
        )
    if schedule.operator is not None:
        _integer(schedule.operator, "operator", 0, 0xFFFFFFFF)


def validate_schedule(
    schedule: FeedingSchedule,
    *,
    tray_count: int | None = None,
    allow_one_shot: bool = False,
) -> None:
    """Check writable settings against app UI limits, separately from decoding."""
    _validate_wire(schedule)
    _integer(schedule.weekdays, "weekdays", 0 if allow_one_shot else 1, 127)
    _integer(
        schedule.tray_compartment,
        "tray_compartment",
        1,
        tray_count if tray_count is not None else 0xFFFFFFFF,
    )
    if (
        schedule.duration_seconds % 60
        or schedule.duration_seconds // 60 not in ALLOWED_DURATION_MINUTES
    ):
        raise ValueError("Duration must be 3–30 whole minutes or 1–12 whole hours")


def schedules_conflict(first: FeedingSchedule, second: FeedingSchedule) -> bool:
    """Mirror the app's weekly 60-minute minimum between enabled starts.

    The app tests start points, independently of feed duration and tray choice.
    A zero weekday mask has no representable recurring occurrence.
    """
    if not first.enabled or not second.enabled:
        return False
    a = [
        day * 1440 + first.hour * 60 + first.minute
        for day in range(7)
        if first.weekdays & (1 << day)
    ]
    b = [
        day * 1440 + second.hour * 60 + second.minute
        for day in range(7)
        if second.weekdays & (1 << day)
    ]
    return any(
        min((left - right) % 10080, (right - left) % 10080)
        < MIN_START_SEPARATION_MINUTES
        for left in a
        for right in b
    )


def validate_schedule_change(
    schedule: FeedingSchedule,
    existing: Iterable[FeedingSchedule],
    *,
    tray_count: int | None = None,
) -> None:
    """Validate an add (id=0) or an update to an existing device-assigned ID."""
    validate_schedule(schedule, tray_count=tray_count)
    current = list(existing)
    if schedule.id:
        if not any(item.id == schedule.id for item in current):
            raise ValueError("Schedule no longer exists; refresh before editing")
        others = [item for item in current if item.id != schedule.id]
    else:
        if len(current) >= MAX_SCHEDULES:
            raise ValueError("The feeder supports at most 10 schedules")
        others = current
    if any(schedules_conflict(schedule, item) for item in others):
        raise ValueError(
            "Enabled schedule start times must be at least 60 minutes apart"
        )


def local_to_utc(
    hour: int, minute: int, iso_weekdays: Iterable[int], utc_offset_minutes: int
) -> tuple[int, int, int]:
    """Convert a local wall time and ISO weekdays (Monday=1) to UTC fields.

    The caller supplies the current UTC offset, matching the app. The stored
    device schedule does not automatically retain a local wall time across DST.
    """
    _integer(hour, "hour", 0, 23)
    _integer(minute, "minute", 0, 59)
    _integer(utc_offset_minutes, "utc_offset_minutes", -1439, 1439)
    days, minutes = divmod(hour * 60 + minute - utc_offset_minutes, 1440)
    mask = 0
    for weekday in iso_weekdays:
        _integer(weekday, "ISO weekday", 1, 7)
        mask |= 1 << ((weekday + days) % 7)
    return minutes // 60, minutes % 60, mask


def utc_to_local(
    hour: int, minute: int, weekdays: int, utc_offset_minutes: int
) -> tuple[int, int, tuple[int, ...]]:
    """Convert device UTC fields to local time and sorted ISO weekdays."""
    _integer(hour, "hour", 0, 23)
    _integer(minute, "minute", 0, 59)
    _integer(weekdays, "weekdays", 0, 127)
    _integer(utc_offset_minutes, "utc_offset_minutes", -1439, 1439)
    days, minutes = divmod(hour * 60 + minute + utc_offset_minutes, 1440)
    local_days = sorted(
        ((day + days - 1) % 7) + 1 for day in range(7) if weekdays & (1 << day)
    )
    return minutes // 60, minutes % 60, tuple(local_days)


def next_occurrences(
    schedule: FeedingSchedule, start: datetime, end: datetime
) -> list[tuple[datetime, datetime]]:
    """Expand recurring UTC events overlapping [start, end).

    A weekday mask of zero is skipped: the wire contract contains no date from
    which a reliable single occurrence could be reconstructed.
    """
    if start.utcoffset() is None or end.utcoffset() is None:
        raise ValueError("Calendar boundaries must be timezone aware")
    start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    _validate_wire(schedule)
    if not schedule.enabled or not schedule.weekdays or end <= start:
        return []
    duration = timedelta(seconds=schedule.duration_seconds)
    day = (start - duration).replace(hour=0, minute=0, second=0, microsecond=0)
    result = []
    while day < end:
        event_start = day.replace(hour=schedule.hour, minute=schedule.minute)
        event_end = event_start + duration
        if (
            schedule.weekdays & (1 << (day.isoweekday() % 7))
            and event_start < end
            and event_end > start
        ):
            result.append((event_start, event_end))
        day += timedelta(days=1)
    return result


def _varint(value: int) -> bytes:
    value &= (1 << 64) - 1
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def _v(tag: int, value: int) -> bytes:
    return _varint(tag << 3) + _varint(value)


def _b(tag: int, value: bytes) -> bytes:
    return _varint((tag << 3) | 2) + _varint(len(value)) + value


def encode_schedules(schedules: Iterable[FeedingSchedule]) -> bytes:
    """Encode the single FeedingScheduleList argument for commands 7 and 9.

    Use validate_schedule_change before submitting user changes. This lower
    level codec also permits decoding/re-encoding older or disabled settings.
    """
    result = bytearray()
    for item in schedules:
        _validate_wire(item)
        start = _v(1, item.hour) + _v(2, item.minute) + item.unknown_start_fields
        options = _v(1, item.tray_compartment) + _v(2, item.duration_seconds)
        for tag, value in (
            (3, item.heat_before_feeding),
            (4, item.heating_temperature),
            (5, item.operator),
        ):
            if value is not None:
                options += _v(tag, int(value))
        options += item.unknown_option_fields
        encoded = (
            _v(1, item.id)
            + _b(2, start)
            + _v(3, int(item.enabled))
            + _v(4, item.weekdays)
            + _b(5, options)
            + item.unknown_fields
        )
        result += _b(1, encoded)
    return bytes(result)


def encode_remove_schedules(ids: Iterable[int]) -> bytes:
    """Encode UInt8List: despite its name, field 1 is packed uint32 values."""
    values = list(ids)
    if not values:
        raise ValueError("Choose at least one schedule to remove")
    return _b(
        1,
        b"".join(
            _varint(_integer(value, "schedule id", 1, 0xFFFFFFFF)) for value in values
        ),
    )


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise ValueError("Truncated schedule protobuf")
        byte = data[offset]
        offset += 1
        if shift == 63 and byte > 1:
            raise ValueError("Oversized protobuf varint")
        value |= (byte & 127) << shift
        if not byte & 128:
            return value, offset
    raise ValueError("Oversized protobuf varint")


def _fields(data: bytes) -> Iterable[tuple[int, int, int | bytes, bytes]]:
    offset = 0
    while offset < len(data):
        begin = offset
        key, offset = _read_varint(data, offset)
        tag, wire = key >> 3, key & 7
        if not tag:
            raise ValueError("Invalid protobuf field zero")
        if wire == 0:
            value, offset = _read_varint(data, offset)
        elif wire in (1, 2, 5):
            if wire == 2:
                size, offset = _read_varint(data, offset)
            else:
                size = 8 if wire == 1 else 4
            if size > len(data) - offset:
                raise ValueError("Truncated schedule protobuf field")
            value, offset = data[offset : offset + size], offset + size
        else:
            raise ValueError("Unsupported protobuf wire type")
        yield tag, wire, value, data[begin:offset]


def _decode_schedule(raw: bytes) -> FeedingSchedule:
    values: dict[str, Any] = {
        "id": 0,
        "hour": 0,
        "minute": 0,
        "weekdays": 0,
        "enabled": False,
        "tray_compartment": 0,
        "duration_seconds": 0,
    }
    unknown = bytearray()
    for tag, wire, value, encoded in _fields(raw):
        if tag in (1, 3, 4) and wire == 0:
            values[{1: "id", 3: "enabled", 4: "weekdays"}[tag]] = (
                bool(value) if tag == 3 else value
            )
        elif tag in (2, 5) and wire == 2:
            extras = bytearray()
            names = (
                {1: "hour", 2: "minute"}
                if tag == 2
                else {
                    1: "tray_compartment",
                    2: "duration_seconds",
                    3: "heat_before_feeding",
                    4: "heating_temperature",
                    5: "operator",
                }
            )
            for inner_tag, inner_wire, inner_value, inner_encoded in _fields(value):
                if inner_tag not in names or inner_wire != 0:
                    extras += inner_encoded
                    continue
                if tag == 5 and inner_tag == 3:
                    inner_value = bool(inner_value)
                elif tag == 5 and inner_tag == 4:
                    inner_value = ((inner_value + (1 << 31)) % (1 << 32)) - (1 << 31)
                values[names[inner_tag]] = inner_value
            values["unknown_start_fields" if tag == 2 else "unknown_option_fields"] = (
                bytes(extras)
            )
        else:
            unknown += encoded
    values["unknown_fields"] = bytes(unknown)
    result = FeedingSchedule(**values)
    _validate_wire(result)
    return result


def _get(data: Mapping[str, Any], name: str, tag: int, default: Any = None) -> Any:
    return data.get(name, data.get(str(tag), data.get(tag, default)))


def _from_json(data: Mapping[str, Any]) -> FeedingSchedule:
    start, options = _get(data, "start", 2, {}), _get(data, "feedOptions", 5, {})
    if not isinstance(start, Mapping) or not isinstance(options, Mapping):
        raise ValueError("Invalid schedule time or feeding options")
    result = FeedingSchedule(
        id=_get(data, "id", 1, 0),
        hour=_get(start, "hour", 1, 0),
        minute=_get(start, "minute", 2, 0),
        enabled=_get(data, "enabled", 3, False),
        weekdays=_get(data, "weekdays", 4, 0),
        tray_compartment=_get(options, "trayCompartmentIndex", 1, 0),
        duration_seconds=_get(options, "feedDurationSeconds", 2, 0),
        heat_before_feeding=_get(options, "heatBeforeFeeding", 3),
        heating_temperature=_get(options, "expectedHeatingTemperature", 4),
        operator=_get(options, "operator", 5),
    )
    _validate_wire(result)
    return result


def decode_schedules(
    value: bytes | str | Mapping[str, Any] | None,
) -> list[FeedingSchedule]:
    """Decode binary MQTT values or named/numeric-tag protobuf JSON shadows."""
    if value is None:
        return []
    if isinstance(value, bytes):
        return [
            _decode_schedule(raw)
            for tag, wire, raw, _ in _fields(value)
            if tag == 1 and wire == 2
        ]
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise ValueError("Invalid schedule list")
    schedules = _get(value, "schedules", 1, [])
    if not isinstance(schedules, list) or not all(
        isinstance(item, Mapping) for item in schedules
    ):
        raise ValueError("Invalid schedule entries")
    return [_from_json(item) for item in schedules]
