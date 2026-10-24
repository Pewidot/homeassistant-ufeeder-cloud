"""Firmware entity tests with fake cloud state; no device operations."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.update import UpdateEntityFeature

from custom_components.ufeeder.button import DESCRIPTIONS, UFeederButton
from custom_components.ufeeder.update import UFeederUpdate


def entity():
    properties = {
        "10:10:1": {"name": "1.2.3", "code": 41},
        "10:10:2": {"name": "1.2.3", "code": 42},
        "10:10:3": 0,
        "10:10:4": 0,
        "10:10:5": False,
        "10:10:6": 0,
    }
    coordinator = SimpleNamespace(
        data={"fake": SimpleNamespace(properties=properties, online=True)},
        transport=SimpleNamespace(connected=True),
        last_update_success=True,
        async_invoke=AsyncMock(),
        async_request_refresh=AsyncMock(),
    )
    return UFeederUpdate(coordinator, "fake"), coordinator, properties


def test_reported_versions_include_build_and_use_app_comparison():
    update, _, properties = entity()
    assert update.installed_version == "v1.2.3.41"
    assert update.latest_version == "v1.2.3.42"
    assert update.state == "on"
    assert update.version_is_newer("v1.10.0.1", "v1.2.99.100")
    assert update.version_is_newer("v1.2.3.42", "v1.2.3.41")
    assert not update.version_is_newer("v1.2.3.41", "v1.2.3.42")
    properties["10:10:2"] = {}
    assert update.latest_version is None


def test_progress_comes_only_from_device_states():
    update, _, properties = entity()
    assert not update.in_progress
    assert update.update_percentage is None
    properties.update({"10:10:3": 1, "10:10:4": 37})
    assert update.in_progress
    assert update.update_percentage == 37
    properties.update({"10:10:3": 4, "10:10:6": 4})
    assert update.in_progress
    assert update.update_percentage is None
    properties["10:10:6"] = 3  # aborted
    assert not update.in_progress


async def test_explicit_check_only_invokes_detection():
    update, coordinator, _ = entity()
    await update.async_update()
    coordinator.async_invoke.assert_awaited_once_with("fake", (10, 10, 1))


async def test_firmware_check_button_only_invokes_detection():
    _, coordinator, _ = entity()
    description = next(item for item in DESCRIPTIONS if item.key == "check_firmware")
    button = UFeederButton(coordinator, "fake", description)
    await button.async_press()
    coordinator.async_invoke.assert_awaited_once_with("fake", (10, 10, 1))


def test_firmware_status_available_without_control_connection():
    update, coordinator, _ = entity()
    coordinator.transport.connected = False
    assert update.available
    assert update.state == "on"
    assert update.supported_features == UpdateEntityFeature.PROGRESS


@pytest.mark.parametrize(
    "version",
    [
        None,
        {},
        {"name": "", "code": 1},
        {"name": 12, "code": 1},
        {"name": "\ud800", "code": 1},
        {"name": "a" * 1025, "code": 1},
        {"name": "1.2.3", "code": "42"},
        {"name": "1.2.3", "code": True},
        {"name": "1.2.3", "code": -1},
        {"name": "1.2.3", "code": 1 << 32},
    ],
)
def test_invalid_reported_version_is_unknown(version):
    update, _, properties = entity()
    properties["10:10:2"] = version
    assert update.latest_version is None
