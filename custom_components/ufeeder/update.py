"""Firmware checks and status reported by the device."""

from __future__ import annotations

from collections.abc import Mapping
import re

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityDescription,
    UpdateEntityFeature,
)

from .controls import COMMAND_PATHS
from .entity import UFeederEntity, setup_entities

DESCRIPTION = UpdateEntityDescription(
    key="firmware",
    translation_key="firmware",
    device_class=UpdateDeviceClass.FIRMWARE,
)
_TRANSFERRING = (1, "TRANSFER_STATE_TRANSFERRING")
_UPDATING = (
    1,
    2,
    4,
    "UPDATE_STATE_VALIDATING_FIRMWARE",
    "UPDATE_STATE_CHECK_PRECONDITION",
    "UPDATE_STATE_UPDATING",
)


def _version(value) -> tuple[str, int] | None:
    if not isinstance(value, Mapping):
        return None
    name, code = value.get("name", value.get("1")), value.get("code", value.get("2", 0))
    if (
        not isinstance(name, str)
        or not name
        or type(code) is not int
        or not 0 <= code <= 0xFFFFFFFF
    ):
        return None
    try:
        if len(name.encode("utf-8")) > 1024:
            return None
    except UnicodeEncodeError:
        return None
    return name, code


def _label(version: tuple[str, int] | None) -> str | None:
    if version is None:
        return None
    name, code = version
    return f"{name if name.startswith('v') else 'v' + name}.{code}"


def _version_key(version: tuple[str, int]) -> tuple[int, ...]:
    # DeviceOtaManager._versionNameSegments takes the first three digit groups,
    # pads with zero, and _isNewerVersion compares code after those segments.
    segments = [int(value) for value in re.findall(r"[0-9]+", version[0])[:3]]
    return (*segments, *([0] * (3 - len(segments))), version[1])


async def async_setup_entry(hass, entry, async_add_entities):
    setup_entities(
        entry,
        async_add_entities,
        lambda coordinator, key: [UFeederUpdate(coordinator, key)],
    )


class UFeederUpdate(UFeederEntity, UpdateEntity):
    """Report firmware availability and progress; installation uses the app."""

    _attr_supported_features = UpdateEntityFeature.PROGRESS

    def __init__(self, coordinator, device_key):
        super().__init__(coordinator, device_key, DESCRIPTION)

    @property
    def installed_version(self):
        return _label(_version(self.value("10:10:1")))

    @property
    def latest_version(self):
        return _label(_version(self.value("10:10:2")))

    @property
    def auto_update(self):
        return self.value("10:10:5") is True

    @property
    def in_progress(self):
        return (
            self.value("10:10:3") in _TRANSFERRING or self.value("10:10:6") in _UPDATING
        )

    @property
    def update_percentage(self):
        if self.value("10:10:3") not in _TRANSFERRING:
            return None
        percentage = self.value("10:10:4")
        return (
            percentage if type(percentage) is int and 0 <= percentage <= 100 else None
        )

    def version_is_newer(self, latest_version: str, installed_version: str) -> bool:
        latest_name, latest_code = latest_version.rsplit(".", 1)
        installed_name, installed_code = installed_version.rsplit(".", 1)
        return _version_key((latest_name, int(latest_code))) > _version_key(
            (installed_name, int(installed_code))
        )

    async def async_update(self):
        """Explicit homeassistant.update_entity requests a firmware check.

        Ordinary coordinator refreshes only read cloud state. Detection can
        finish after its command response; MQTT/shadow updates supply its result.
        """
        if self.enabled:
            await self.coordinator.async_invoke(
                self.device_key, COMMAND_PATHS["detect_firmware_update"]
            )
