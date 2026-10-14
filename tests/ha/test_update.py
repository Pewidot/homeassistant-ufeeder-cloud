"""Firmware entity tests with fake cloud state; no device operations."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

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


@pytest.mark.parametrize(
    ("key", "arguments"),
    (
        ("check_firmware", ("fake", (10, 10, 1))),
        ("stop_firmware_download", ("fake", (10, 10, 3), b"\x08\x01")),
    ),
)
async def test_firmware_buttons_use_recovered_arguments(key, arguments):
    _, coordinator, _ = entity()
    description = next(item for item in DESCRIPTIONS if item.key == key)
    button = UFeederButton(coordinator, "fake", description)
    await button.async_press()
    coordinator.async_invoke.assert_awaited_once_with(*arguments)


async def test_install_sends_only_reported_version_after_refresh():
    update, coordinator, _ = entity()
    await update.async_install(None, False)
    coordinator.async_request_refresh.assert_awaited_once()
    coordinator.async_invoke.assert_awaited_once_with(
        "fake", (10, 10, 2), bytes.fromhex("0a05312e322e33102a")
    )
    assert not update.in_progress  # command acknowledgement does not invent progress


async def test_changed_or_non_newer_target_is_never_downloaded():
    for new_code in (41, 43):
        update, coordinator, properties = entity()

        async def refresh():
            properties["10:10:2"]["code"] = new_code

        coordinator.async_request_refresh.side_effect = refresh
        with pytest.raises(ServiceValidationError):
            await update.async_install(None, False)
        coordinator.async_invoke.assert_not_awaited()


async def test_invalid_install_options_and_active_update_rejected():
    for version, backup, state in (
        ("v9.9.9.1", False, 0),
        (None, True, 0),
        (None, False, 4),
    ):
        update, coordinator, properties = entity()
        properties["10:10:6"] = state
        with pytest.raises(ServiceValidationError):
            await update.async_install(version, backup)
        coordinator.async_invoke.assert_not_awaited()


async def test_command_failure_is_not_retried():
    update, coordinator, _ = entity()
    coordinator.async_invoke.side_effect = HomeAssistantError("Response timed out")
    with pytest.raises(HomeAssistantError):
        await update.async_install(None, False)
    assert coordinator.async_invoke.await_count == 1


async def test_parallel_install_is_rejected():
    update, coordinator, _ = entity()
    started, release = asyncio.Event(), asyncio.Event()

    async def refresh():
        started.set()
        await release.wait()

    coordinator.async_request_refresh.side_effect = refresh
    first = asyncio.create_task(update.async_install(None, False))
    await started.wait()
    try:
        with pytest.raises(ServiceValidationError):
            await update.async_install(None, False)
    finally:
        release.set()
        await first
    assert coordinator.async_invoke.await_count == 1
