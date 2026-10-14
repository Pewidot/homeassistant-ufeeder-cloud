"""Real HA config/option flows and entity lifecycle, with mocked device I/O."""

import asyncio
import logging
from collections import Counter
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.components.calendar.const import DATA_COMPONENT as CALENDAR_COMPONENT
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER, ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, STATE_UNAVAILABLE
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ufeeder.api import ApiError, FeederState, InvalidAuth
from custom_components.ufeeder.const import (
    CONF_CLIENT_ID,
    CONF_REGION,
    CONF_SESSION,
    DOMAIN,
)
from custom_components.ufeeder.schedules import FeedingSchedule, decode_schedules
from custom_components.ufeeder.schema import vol
from custom_components.ufeeder.transport import parse_fields

ACCOUNT_ID = "ucsp-eu.ubtrobot.com:12345"
LOGIN = {
    CONF_USERNAME: "test@example.test",
    CONF_PASSWORD: "not-a-real-password",
    CONF_REGION: "DE",
}
SESSION = {
    "token": "test-access",
    "refreshToken": "test-refresh",
    "userId": "12345",
    "domain": "https://ucsp-eu.ubtrobot.com",
    "expireAt": 9999999999,
    "refreshExpireAt": 9999999999,
}


@pytest.fixture
def devices():
    entry = FeedingSchedule(
        id=7,
        hour=8,
        weekdays=127,
        tray_compartment=2,
        duration_seconds=600,
        heat_before_feeding=True,
        heating_temperature=28,
        operator=12345,
    )
    properties = {
        "10:11:1": 1,
        "10:11:2": 0,
        "10:11:3": 0,
        "10:11:4": 1,
        "10:11:5": 3,
        "10:11:6": 2,
        "10:11:7": {
            "feedDurationSeconds": 600,
            "heatBeforeFeeding": True,
            "expectedHeatingTemperature": 28,
        },
        "10:11:8": True,
        "10:11:9": False,
        "10:11:10": True,
        "10:11:11": 17,
        "10:11:12": 17,
        "10:11:13": {"schedules": [entry.as_dict()]},
        "10:11:14": {},
        "10:11:16": 0,
        "10:11:17": True,
        "10:11:18": 0,
        "10:11:19": 15,
        "10:11:20": 16,
        "10:11:22": False,
        "10:11:23": 30,
        "10:10:1": {"name": "test-firmware", "code": 1},
        "10:10:5": False,
        "10:10:7": 1,
        "10:10:8": 95,
    }
    return [
        FeederState(
            "test-online", "Kitchen feeder", online=True, properties=properties
        ),
        FeederState(
            "test-offline",
            "Guest feeder",
            online=False,
            properties=deepcopy(properties),
        ),
    ]


@pytest.fixture
def api(devices):
    fake = Mock()
    fake.unique_id = ACCOUNT_ID
    fake.user_id = "12345"
    fake.username = LOGIN[CONF_USERNAME]
    fake.region = "DE"
    fake.async_login = AsyncMock()
    fake.async_devices = AsyncMock(side_effect=lambda: deepcopy(devices))
    fake.async_device_state = AsyncMock(side_effect=lambda device: device)
    fake.export_session.return_value = SESSION.copy()
    return fake


@pytest.fixture
def transport():
    fake = Mock()
    fake.connected = True
    for method in (
        "async_start",
        "async_stop",
        "async_set_devices",
        "async_invoke",
        "async_write",
    ):
        setattr(fake, method, AsyncMock())
    return fake


@pytest.fixture
def history_fetch():
    value = {
        "last_feeding": datetime(2026, 10, 13, 7, 30, tzinfo=UTC),
        "last_end": datetime(2026, 10, 13, 7, 40, tzinfo=UTC),
        "last_compartment": 2,
        "last_status": "end",
        "last_source": "schedule",
        "feed_count_today": 3,
        "recent_records": [],
        "history_truncated": False,
        "history_since": datetime(2026, 9, 13, tzinfo=UTC),
        "fetched_at": datetime(2026, 10, 13, 8, tzinfo=UTC),
    }
    return AsyncMock(side_effect=lambda api, device: deepcopy(value))


@pytest.fixture
async def loaded_entry(hass, api, transport, history_fetch, caplog):
    await hass.config.async_set_time_zone("UTC")
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ACCOUNT_ID,
        data={**LOGIN, CONF_CLIENT_ID: "test-client-id", CONF_SESSION: SESSION.copy()},
    )
    entry.add_to_hass(hass)
    setup_log_start = len(caplog.records)
    with (
        patch("custom_components.ufeeder.UFeederApi", return_value=api),
        patch(
            "custom_components.ufeeder.coordinator.UFeederTransport",
            return_value=transport,
        ),
        patch(
            "custom_components.ufeeder.coordinator.async_fetch_history", history_fetch
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        # HA can mark an entry loaded even when an individual entity platform
        # fails. No setup-time error is expected with all external I/O mocked.
        setup_errors = [
            f"{record.name}: {record.getMessage()}"
            for record in caplog.records[setup_log_start:]
            if record.levelno >= logging.ERROR
        ]
        assert not setup_errors, setup_errors
        yield entry
        if entry.state is ConfigEntryState.LOADED:
            assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_config_flow_success_and_duplicate(hass, api):
    with (
        patch("custom_components.ufeeder.config_flow.UFeederApi", return_value=api),
        patch(
            "custom_components.ufeeder.async_setup_entry", AsyncMock(return_value=True)
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], LOGIN
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["data"][CONF_SESSION] == SESSION
        assert result["data"][CONF_CLIENT_ID]
        assert result["result"].unique_id == ACCOUNT_ID
        await hass.async_block_till_done()
        second = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}, data=LOGIN
        )
        assert second["type"] is FlowResultType.ABORT
        assert second["reason"] == "already_configured"


async def test_config_flow_bad_auth(hass, api):
    api.async_login.side_effect = InvalidAuth("test error")
    with patch("custom_components.ufeeder.config_flow.UFeederApi", return_value=api):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}, data=LOGIN
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    api.async_devices.assert_not_awaited()
    assert not hass.config_entries.async_entries(DOMAIN)


@pytest.mark.parametrize("account_matches", [True, False])
async def test_reauth_preserves_account_identity(hass, api, account_matches):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ACCOUNT_ID,
        data={
            **LOGIN,
            CONF_CLIENT_ID: "persistent-client",
            CONF_SESSION: SESSION.copy(),
        },
    )
    entry.add_to_hass(hass)
    if not account_matches:
        api.unique_id = "ucsp-eu.ubtrobot.com:different-user"
    with (
        patch(
            "custom_components.ufeeder.config_flow.UFeederApi", return_value=api
        ) as factory,
        patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=entry.data,
        )
        assert result["step_id"] == "reauth_confirm"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**LOGIN, CONF_PASSWORD: "replacement-test-password"}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == (
        "reauth_successful" if account_matches else "wrong_account"
    )
    assert factory.call_args.args[4] == "persistent-client"
    assert entry.data[CONF_PASSWORD] == (
        "replacement-test-password" if account_matches else LOGIN[CONF_PASSWORD]
    )


def entity_id(hass, device, platform, key):
    registry = er.async_get(hass)
    result = registry.async_get_entity_id(platform, DOMAIN, f"{device.key}:{key}")
    assert result is not None
    return result


async def test_setup_availability_mqtt_disconnect_and_unload(
    hass, loaded_entry, devices, transport
):
    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, loaded_entry.entry_id)
    expected_counts = {
        "binary_sensor": 2,
        "sensor": 18,
        "switch": 6,
        "select": 3,
        "button": 5,
        "calendar": 1,
        "update": 1,
    }
    assert len(entries) == 72
    for device in devices:
        device_entries = [
            entry
            for entry in entries
            if entry.device_id == registry_device_id(hass, device)
        ]
        assert Counter(entry.domain for entry in device_entries) == expected_counts
        for entry in device_entries:
            if entry.disabled_by is not None:
                continue
            state = hass.states.get(entry.entity_id)
            assert state is not None, entry.entity_id
            assert not state.attributes.get("restored"), entry.entity_id
    online, offline = devices
    temperature = entity_id(hass, online, "sensor", "temperature")
    offline_temperature = entity_id(hass, offline, "sensor", "temperature")
    control = entity_id(hass, online, "switch", "auto_feeding")
    assert hass.states.get(temperature).state == "17"
    assert hass.states.get(offline_temperature).state == STATE_UNAVAILABLE
    assert hass.states.get(control).state == "on"
    transport.connected = False
    loaded_entry.runtime_data.async_update_listeners()
    await hass.async_block_till_done()
    assert hass.states.get(temperature).state == "17"
    assert hass.states.get(control).state == STATE_UNAVAILABLE
    assert await hass.config_entries.async_unload(loaded_entry.entry_id)
    await hass.async_block_till_done()
    transport.async_stop.assert_awaited_once()
    assert loaded_entry.state is ConfigEntryState.NOT_LOADED
    # HA retains an unavailable registry placeholder after unloading entities.
    restored = hass.states.get(temperature)
    assert restored.state == STATE_UNAVAILABLE
    assert restored.attributes["restored"] is True


async def open_options(hass, entry, device, action):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"device": device.key}
    )
    assert result["type"] is FlowResultType.MENU
    assert action in result["menu_options"]
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": action}
    )


async def test_options_add_schedule_excludes_manual_heater_fields(
    hass, loaded_entry, devices
):
    coordinator = loaded_entry.runtime_data
    with patch.object(coordinator, "async_save_schedule", AsyncMock()) as save:
        result = await open_options(hass, loaded_entry, devices[0], "add_schedule")
        assert result["step_id"] == "schedule"
        assert {str(key) for key in result["data_schema"].schema} == {
            "time",
            "weekdays",
            "enabled",
            "tray",
            "duration_minutes",
        }
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                "time": "10:30:00",
                "weekdays": ["1", "3", "5"],
                "enabled": True,
                "tray": "3",
                "duration_minutes": "10",
            },
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert save.await_args.args == (
        devices[0].key,
        FeedingSchedule(
            hour=10, minute=30, weekdays=42, tray_compartment=3, duration_seconds=600
        ),
    )


async def test_options_edit_preserves_id_and_optional_settings(
    hass, loaded_entry, devices
):
    with patch.object(
        loaded_entry.runtime_data, "async_save_schedule", AsyncMock()
    ) as save:
        result = await open_options(hass, loaded_entry, devices[0], "edit_schedule")
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"schedule": "7"}
        )
        assert result["step_id"] == "schedule"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                "time": "09:15:00",
                "weekdays": ["2", "4"],
                "enabled": False,
                "tray": "1",
                "duration_minutes": "60",
            },
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    saved = save.await_args.args[1]
    assert (saved.id, saved.hour, saved.minute, saved.weekdays, saved.enabled) == (
        7,
        9,
        15,
        20,
        False,
    )
    assert (saved.heat_before_feeding, saved.heating_temperature, saved.operator) == (
        True,
        28,
        12345,
    )


async def test_options_delete_requires_confirmation(hass, loaded_entry, devices):
    with patch.object(
        loaded_entry.runtime_data, "async_delete_schedule", AsyncMock()
    ) as delete:
        result = await open_options(hass, loaded_entry, devices[0], "delete_schedule")
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"schedule": "7"}
        )
        assert result["step_id"] == "confirm_delete"
        delete.assert_not_awaited()
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    delete.assert_awaited_once_with(devices[0].key, 7)


async def test_options_manual_feed_payload_and_command_failure(
    hass, loaded_entry, devices
):
    with patch.object(
        loaded_entry.runtime_data,
        "async_invoke",
        AsyncMock(side_effect=[HomeAssistantError("test"), None]),
    ) as invoke:
        result = await open_options(hass, loaded_entry, devices[0], "feed_now")
        form = {
            "tray": "2",
            "duration_minutes": "10",
            "preheat": True,
            "temperature": "24",
        }
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], form
        )
        assert result["errors"] == {"base": "command_failed"}
        assert invoke.await_count == 1  # No automatic replay of an ambiguous command.
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], form
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    key, command, argument = invoke.await_args.args
    assert (key, command) == (devices[0].key, (10, 11, 2))
    assert parse_fields(argument) == [
        (1, 0, 2),
        (2, 0, 600),
        (3, 0, 1),
        (4, 0, 24),
        (5, 0, 12345),
    ]


async def test_options_rejects_seconds_before_command(hass, loaded_entry, devices):
    with patch.object(
        loaded_entry.runtime_data, "async_save_schedule", AsyncMock()
    ) as save:
        result = await open_options(hass, loaded_entry, devices[0], "add_schedule")
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                "time": "10:30:15",
                "weekdays": ["1"],
                "enabled": True,
                "tray": "3",
                "duration_minutes": "10",
            },
        )
    assert result["errors"] == {"base": "invalid_schedule"}
    save.assert_not_awaited()


@pytest.mark.parametrize(
    "condition", ["offline", "battery", "updating", "pairing", "mqtt_disconnected"]
)
async def test_coordinator_rejects_unsafe_or_unavailable_commands(
    loaded_entry, devices, transport, condition
):
    coordinator = loaded_entry.runtime_data
    device = coordinator.data[devices[0].key]
    if condition == "offline":
        device.online = False
    elif condition == "battery":
        device.properties["10:10:7"] = 0
    elif condition == "updating":
        device.properties["10:11:1"] = 4
    elif condition == "pairing":
        device.properties["10:11:1"] = 2
    else:
        transport.connected = False
    with pytest.raises(ServiceValidationError):
        await coordinator.async_invoke(device.key, (10, 11, 2), b"test")
    transport.async_invoke.assert_not_awaited()


async def test_coordinator_blocks_edit_of_executing_schedule(
    loaded_entry, devices, api, transport
):
    coordinator = loaded_entry.runtime_data
    entry = decode_schedules(devices[0].properties["10:11:13"])[0]
    current = deepcopy(devices[0])
    current.properties["10:11:14"] = {"schedules": [entry.as_dict()]}
    api.async_device_state.side_effect = None
    api.async_device_state.return_value = current
    with pytest.raises(ServiceValidationError, match="executing"):
        await coordinator.async_save_schedule(current.key, replace(entry, hour=10))
    transport.async_invoke.assert_not_awaited()


async def test_coordinator_serializes_per_device_commands(
    loaded_entry, devices, transport
):
    coordinator = loaded_entry.runtime_data
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold_first(*args):
        entered.set()
        await release.wait()

    transport.async_invoke.side_effect = hold_first
    with patch.object(coordinator, "async_request_refresh", AsyncMock()):
        first = asyncio.create_task(
            coordinator.async_invoke(devices[0].key, (10, 11, 3))
        )
        await entered.wait()
        second = asyncio.create_task(
            coordinator.async_invoke(devices[0].key, (10, 11, 4))
        )
        await asyncio.sleep(0)
        assert transport.async_invoke.await_count == 1
        release.set()
        await asyncio.gather(first, second)
    assert transport.async_invoke.await_count == 2


async def test_calendar_expands_weekly_schedules_and_auto_feeding_gate(
    hass, loaded_entry, devices
):
    coordinator = loaded_entry.runtime_data
    calendars = []
    for device in devices:
        identifier = entity_id(hass, device, "calendar", "feeding_schedule")
        calendar = hass.data[CALENDAR_COMPONENT].get_entity(identifier)
        assert calendar is not None
        # CalendarEntity accesses this specialized description while registering
        # its options. A generic EntityDescription silently loses the platform.
        assert calendar.entity_description.initial_color is None
        assert calendar.initial_color is None
        assert calendar.get_initial_entity_options() is None
        calendars.append(calendar)
    assert hass.states.get(calendars[0].entity_id).state in ("on", "off")
    assert hass.states.get(calendars[1].entity_id).state == STATE_UNAVAILABLE
    calendar = calendars[0]
    start = datetime(2026, 10, 12, tzinfo=UTC)
    end = datetime(2026, 10, 14, tzinfo=UTC)
    events = await calendar.async_get_events(hass, start, end)
    assert len(events) == 2
    assert events[0].uid == "7"
    assert events[0].start.hour == 8
    coordinator.data[devices[0].key].properties["10:11:17"] = False
    assert await calendar.async_get_events(hass, start, end) == []


def registry_device_id(hass, feeder):
    devices = dr.async_get(hass).async_get_devices(identifiers={(DOMAIN, feeder.key)})
    assert len(devices) == 1
    return devices[0].id


async def test_feed_service_routes_exactly_one_command(
    hass, loaded_entry, devices, transport
):
    coordinator = loaded_entry.runtime_data
    # The same shared feeder may be visible through another account. The service
    # must choose a single loaded coordinator, never fan out feeding commands.
    duplicate = MockConfigEntry(
        domain=DOMAIN, unique_id="different-test-account", data={}
    )
    duplicate.add_to_hass(hass)
    duplicate.runtime_data = SimpleNamespace(
        data=coordinator.data, async_invoke=AsyncMock()
    )
    with patch.object(coordinator, "async_request_refresh", AsyncMock()):
        await hass.services.async_call(
            DOMAIN,
            "feed",
            {
                "device_id": registry_device_id(hass, devices[0]),
                "compartment": 2.0,
                "duration_minutes": 10.0,
                "preheat": True,
                "temperature": "24",
            },
            blocking=True,
        )
    transport.async_invoke.assert_awaited_once()
    device, command, argument = transport.async_invoke.await_args.args
    assert (device, command) == (devices[0].device_id, (10, 11, 2))
    assert parse_fields(argument) == [
        (1, 0, 2),
        (2, 0, 600),
        (3, 0, 1),
        (4, 0, 24),
        (5, 0, 12345),
    ]
    duplicate.runtime_data.async_invoke.assert_not_awaited()


async def test_feed_service_uses_selected_device_account(
    hass, loaded_entry, devices, transport
):
    coordinator = loaded_entry.runtime_data
    duplicate = MockConfigEntry(
        domain=DOMAIN, unique_id="different-test-account", data={}
    )
    duplicate.add_to_hass(hass)
    duplicate.runtime_data = SimpleNamespace(
        data=coordinator.data,
        async_invoke=AsyncMock(),
        _check_writable=Mock(return_value=devices[0]),
        api=SimpleNamespace(user_id="67890"),
    )
    selected = dr.async_get(hass).async_get_or_create(
        config_entry_id=duplicate.entry_id, identifiers={(DOMAIN, devices[0].key)}
    )
    # HA scopes device identifiers to their config entry. Selecting the second
    # account's registry device must not issue the request through the first.
    with patch.object(coordinator, "async_request_refresh", AsyncMock()):
        await hass.services.async_call(
            DOMAIN,
            "feed",
            {
                "device_id": selected.id,
                "compartment": 2,
            },
            blocking=True,
        )
    duplicate.runtime_data.async_invoke.assert_awaited_once()
    key, command, argument = duplicate.runtime_data.async_invoke.await_args.args
    assert (key, command) == (devices[0].key, (10, 11, 2))
    assert parse_fields(argument)[-1] == (5, 0, 67890)
    transport.async_invoke.assert_not_awaited()


@pytest.mark.parametrize(
    "condition",
    [
        "missing_device",
        "offline",
        "unloaded",
        "compartment_zero",
        "compartment_not_inserted",
        "compartment_too_high",
        "compartment_fraction",
        "compartment_boolean",
        "duration_fraction",
        "temperature_fraction",
    ],
)
async def test_feed_service_invalid_targets_never_feed(
    hass, loaded_entry, devices, transport, condition
):
    target = devices[1] if condition == "offline" else devices[0]
    data = {"device_id": registry_device_id(hass, target), "compartment": 2}
    if condition == "missing_device":
        data["device_id"] = "does-not-exist"
    elif condition == "unloaded":
        loaded_entry.runtime_data.data.clear()
    elif condition == "compartment_zero":
        data["compartment"] = 0
    elif condition == "compartment_not_inserted":
        data["compartment"] = 4  # A valid protocol index, absent from this 3-cell tray.
    elif condition == "compartment_too_high":
        data["compartment"] = 5
    elif condition == "compartment_fraction":
        data["compartment"] = 2.5
    elif condition == "compartment_boolean":
        data["compartment"] = True
    elif condition == "duration_fraction":
        data["duration_minutes"] = 3.5
    elif condition == "temperature_fraction":
        data["temperature"] = 24.5
    with pytest.raises((HomeAssistantError, vol.Invalid)):
        await hass.services.async_call(DOMAIN, "feed", data, blocking=True)
    transport.async_invoke.assert_not_awaited()


async def test_history_sensors_have_timestamp_and_total_increasing_state(
    hass, loaded_entry, devices
):
    online, offline = devices
    count = hass.states.get(entity_id(hass, online, "sensor", "feed_count_today"))
    last = hass.states.get(entity_id(hass, online, "sensor", "last_feeding"))
    assert count.state == "3"
    assert count.attributes["state_class"] == "total_increasing"
    assert last.state == "2026-10-13T07:30:00+00:00"
    assert last.attributes["device_class"] == "timestamp"
    assert last.attributes["last_compartment"] == 2
    assert (
        hass.states.get(entity_id(hass, online, "sensor", "last_feeding_status")).state
        == "end"
    )
    assert (
        hass.states.get(entity_id(hass, online, "sensor", "last_feeding_source")).state
        == "schedule"
    )
    assert (
        hass.states.get(entity_id(hass, offline, "sensor", "last_feeding")).state
        == STATE_UNAVAILABLE
    )


async def test_history_failure_keeps_controls_and_cached_history(
    hass, loaded_entry, devices, transport, history_fetch
):
    coordinator = loaded_entry.runtime_data
    previous = deepcopy(coordinator.data[devices[0].key].history)
    history_fetch.side_effect = ApiError(403)
    coordinator._history_last_fetch.clear()
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.last_update_success
    assert coordinator.data[devices[0].key].history == previous
    assert (
        hass.states.get(entity_id(hass, devices[0], "switch", "auto_feeding")).state
        == "on"
    )
    with patch.object(coordinator, "async_request_refresh", AsyncMock()):
        await coordinator.async_invoke(devices[0].key, (10, 11, 3))
    transport.async_invoke.assert_awaited_once()
    # No old history is also valid: history stays unknown, telemetry and control
    # are still available when a shared account lacks history permissions.
    coordinator._history_cache.clear()
    coordinator._history_last_fetch.clear()
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.last_update_success
    assert coordinator.data[devices[0].key].history == {}
    assert (
        hass.states.get(entity_id(hass, devices[0], "sensor", "last_feeding")).state
        == "unknown"
    )
    assert (
        hass.states.get(entity_id(hass, devices[0], "sensor", "temperature")).state
        == "17"
    )


async def test_history_cache_expiry_and_event_invalidation(
    hass, loaded_entry, devices, history_fetch
):
    coordinator = loaded_entry.runtime_data
    initial_calls = history_fetch.await_count
    await coordinator.async_refresh()
    assert history_fetch.await_count == initial_calls
    # Force one device beyond the five-minute cache TTL, leaving its peer fresh.
    coordinator._history_last_fetch[devices[0].key] -= 301
    await coordinator.async_refresh()
    assert history_fetch.await_count == initial_calls + 1
    with patch.object(coordinator, "_delayed_refresh", AsyncMock()) as delayed:
        coordinator._report(
            devices[0].device_id,
            SimpleNamespace(events=[object()], attributes={}, timestamp=1),
        )
        await hass.async_block_till_done()
        delayed.assert_awaited_once()
    assert coordinator._history_last_fetch[devices[0].key] == -300
    await coordinator.async_refresh()
    assert history_fetch.await_count == initial_calls + 2


async def test_history_midnight_drops_previous_day_counter_on_failure(
    hass, loaded_entry, devices, history_fetch
):
    coordinator = loaded_entry.runtime_data
    tomorrow = datetime.combine(
        coordinator._history_day[devices[0].key],
        datetime.min.time(),
        tzinfo=UTC,
    ).replace(hour=1)
    tomorrow += timedelta(days=1)
    history_fetch.side_effect = ApiError(503)
    with patch(
        "custom_components.ufeeder.coordinator.dt_util",
        SimpleNamespace(now=lambda: tomorrow),
    ):
        await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.last_update_success
    # Yesterday's total must not be reported as today's count after rollover.
    assert coordinator.data[devices[0].key].history == {}
    state = hass.states.get(entity_id(hass, devices[0], "sensor", "feed_count_today"))
    assert state.state == "unknown"
    assert (
        hass.states.get(entity_id(hass, devices[0], "switch", "auto_feeding")).state
        == "on"
    )
