"""Pure protobuf payload builders for the app's non-schedule controls.

Paths, field numbers and UI limits were recovered from the Android APK. These
functions do not perform network operations.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

PROPERTY_PATHS = {
    "preferences": (10, 11, 7),
    "pet_detection": (10, 11, 8),
    "temperature_control": (10, 11, 10),
    "cooling_temperature": (10, 11, 12),
    "auto_feeding": (10, 11, 17),
    "heat_before_feed_now": (10, 11, 22),
    "automatic_update": (10, 10, 5),
}
COMMAND_PATHS = {
    "feed_now": (10, 11, 2),
    "skip_heating": (10, 11, 3),
    "stop_feeding": (10, 11, 4),
    "detect_firmware_update": (10, 10, 1),
    "start_firmware_download": (10, 10, 2),
    "stop_firmware_download": (10, 10, 3),
}
COOLING_TEMPERATURES = (14, 17, 20)
HEATING_TEMPERATURES = (20, 24, 28)
DURATION_MINUTES = (*range(3, 31), *range(60, 721, 60))
STOP_FIRMWARE_DOWNLOAD_ARGUMENT = b"\x08\x01"

_BOOL_PROPERTIES = frozenset(
    path
    for name, path in PROPERTY_PATHS.items()
    if name not in ("preferences", "cooling_temperature")
)
_PREFERENCE_KEYS = frozenset(
    (
        "feedDurationSeconds",
        "heatBeforeFeeding",
        "expectedHeatingTemperature",
        "1",
        "2",
        "3",
    )
)


def _integer(value: Any, name: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer in {low}..{high}")
    return value


def _boolean(value: Any, name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{name} must be a boolean")
    return value


def _varint(value: int) -> bytes:
    if value < 0:
        value &= (1 << 64) - 1
    output = bytearray()
    while value >= 128:
        output.append((value & 127) | 128)
        value >>= 7
    output.append(value)
    return bytes(output)


def _field(number: int, value: int | bool) -> bytes:
    return _varint(number << 3) + _varint(int(value))


def validate_duration_minutes(value: Any) -> int:
    """Accept the app's minute selector and whole-hour selector choices."""
    _integer(value, "duration_minutes", 3, 720)
    if value not in DURATION_MINUTES:
        raise ValueError("Duration must be 3–30 whole minutes or 1–12 whole hours")
    return value


def _validate_duration_seconds(value: Any) -> int:
    _integer(value, "duration_seconds", 180, 43200)
    if value % 60:
        raise ValueError("Duration must use whole minutes")
    validate_duration_minutes(value // 60)
    return value


def _temperature(value: Any, choices: tuple[int, ...], name: str) -> int:
    if type(value) is not int or value not in choices:
        raise ValueError(f"{name} must be one of {choices}")
    return value


def encode_scalar_property(path: tuple[int, int, int], value: Any) -> bytes:
    """Encode an allowlisted writable BoolValue or Int32Value property.

    Proto3 BoolValue false has an empty payload. The transport must still send
    the enclosing attribute value, including its zero byte length.
    """
    if path in _BOOL_PROPERTIES:
        return b"\x08\x01" if _boolean(value, "value") else b""
    if path == PROPERTY_PATHS["cooling_temperature"]:
        return _field(
            1, _temperature(value, COOLING_TEMPERATURES, "cooling_temperature")
        )
    raise ValueError("Property is not a supported writable scalar")


def encode_feed_options(
    *,
    tray: int,
    compartment_count: int,
    duration_seconds: int,
    preheat: bool = False,
    temperature: int = 24,
    user_id: int | str,
) -> bytes:
    """Build FeedOptions for feed-now, using a one-based tray number.

    The app supports three- and four-compartment trays. Its feed-now default
    temperature is 24 °C; operator is the numeric authenticated user ID.
    """
    if type(compartment_count) is not int or compartment_count not in (3, 4):
        raise ValueError("The feeder must report a three- or four-compartment tray")
    _integer(tray, "tray", 1, compartment_count)
    _validate_duration_seconds(duration_seconds)
    _boolean(preheat, "preheat")
    _temperature(temperature, HEATING_TEMPERATURES, "temperature")
    if isinstance(user_id, str) and user_id.isascii() and user_id.isdecimal():
        user_id = int(user_id)
    _integer(user_id, "user_id", 1, 0xFFFFFFFF)
    return b"".join(
        (
            _field(1, tray),
            _field(2, duration_seconds),
            _field(3, preheat),
            _field(4, temperature),
            _field(5, user_id),
        )
    )


def _preference(current: Mapping[str, Any], name: str, tag: str, default: Any) -> Any:
    if name in current and tag in current and current[name] != current[tag]:
        raise ValueError("Conflicting named and numeric preference fields")
    return current.get(name, current.get(tag, default))


def encode_preferences(
    current: Mapping[str, Any],
    *,
    duration_minutes: int | None = None,
    heat_before_feeding: bool | None = None,
    heating_temperature: int | None = None,
) -> bytes:
    """Merge changes into a fresh shadow value before writing all preferences.

    Accept named or numeric protobuf JSON. Existing fields retain their wire
    values, including defaults omitted by protobuf JSON; UI limits apply to
    changed fields only. Reject unknown fields rather than dropping settings
    introduced by newer firmware. The caller's mapping is never mutated.
    """
    if not isinstance(current, Mapping):
        raise ValueError("Current feed preferences must be a mapping")
    if set(current) - _PREFERENCE_KEYS:
        raise ValueError("Feed preferences contain unsupported fields")
    if not current and duration_minutes is None:
        raise ValueError("Refresh feed preferences before changing them")
    duration = _preference(current, "feedDurationSeconds", "1", 0)
    heat = _preference(current, "heatBeforeFeeding", "2", False)
    temperature = _preference(current, "expectedHeatingTemperature", "3", 0)
    if duration_minutes is not None:
        duration = validate_duration_minutes(duration_minutes) * 60
    if heat_before_feeding is not None:
        heat = _boolean(heat_before_feeding, "heat_before_feeding")
    if heating_temperature is not None:
        temperature = _temperature(
            heating_temperature, HEATING_TEMPERATURES, "heating_temperature"
        )
    _integer(duration, "feedDurationSeconds", 0, 0xFFFFFFFF)
    _boolean(heat, "heatBeforeFeeding")
    _integer(temperature, "expectedHeatingTemperature", -(1 << 31), (1 << 31) - 1)
    return _field(1, duration) + _field(2, heat) + _field(3, temperature)


def encode_firmware_version(name: str, code: int) -> bytes:
    """Encode a device/cloud-reported FirmwareVersion for start-download.

    Do not synthesize code from a version name or supply an arbitrary version.
    The app first invokes firmware detection and awaits the detected version.
    """
    if not isinstance(name, str) or not name or len(name.encode("utf-8")) > 1024:
        raise ValueError("Firmware version name must be a nonempty string")
    _integer(code, "code", 0, 0xFFFFFFFF)
    value = name.encode("utf-8")
    return b"\x0a" + _varint(len(value)) + value + _field(2, code)
