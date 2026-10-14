"""Explicit firmware checks and downloads using the device's reported version."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import re

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityDescription,
    UpdateEntityFeature,
)
from homeassistant.exceptions import ServiceValidationError

from .controls import COMMAND_PATHS, encode_firmware_version
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
    try:
        encode_firmware_version(name, code)
    except (ValueError, TypeError):
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
    """The app's download command initiates its firmware-update flow.

    There is no separately recovered install/reboot command. Completion and
    percentage come only from device reports. No operation is replayed.
    """

    control = True
    _attr_supported_features = (
        UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS
    )

    def __init__(self, coordinator, device_key):
        super().__init__(coordinator, device_key, DESCRIPTION)
        self._install_lock = asyncio.Lock()

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

    async def async_install(self, version: str | None, backup: bool, **kwargs):
        if backup:
            raise ServiceValidationError(
                "The feeder does not expose a firmware backup operation"
            )
        if self._install_lock.locked() or self.in_progress:
            raise ServiceValidationError("A firmware update is already in progress")
        target = _version(self.value("10:10:2"))
        if target is None:
            raise ServiceValidationError("Check for firmware updates before installing")
        if version is not None and version != _label(target):
            raise ServiceValidationError(
                "Only the firmware detected by this feeder can be installed"
            )
        async with self._install_lock:
            # A fresh read avoids using an old detected version from HA's cache.
            # The mobile app also skips detection when the target already matches.
            await self.coordinator.async_request_refresh()
            if not self.coordinator.last_update_success:
                raise ServiceValidationError(
                    "Could not refresh the feeder's firmware state"
                )
            if _version(self.value("10:10:2")) != target:
                raise ServiceValidationError(
                    "Detected firmware changed; review the new version first"
                )
            current = _version(self.value("10:10:1"))
            if current is None or _version_key(target) <= _version_key(current):
                raise ServiceValidationError("No newer detected firmware is available")
            if self.in_progress:
                raise ServiceValidationError("A firmware update is already in progress")
            await self.coordinator.async_invoke(
                self.device_key,
                COMMAND_PATHS["start_firmware_download"],
                encode_firmware_version(*target),
            )
