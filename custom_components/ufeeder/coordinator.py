"""Account discovery, push updates, and serialized device operations."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from copy import deepcopy
from datetime import timedelta
import logging
import time

from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import InvalidAuth, UFeederError
from .const import DOMAIN, POLL_SECONDS
from .history import async_fetch_history
from .transport import ProtocolError, UFeederTransport, decode_scalar

_LOGGER = logging.getLogger(__name__)
BOOL_PROPERTIES = frozenset(
    ("10:11:8", "10:11:9", "10:11:10", "10:11:17", "10:11:22", "10:10:5")
)
STRUCT_PROPERTIES = frozenset(
    ("10:11:7", "10:11:13", "10:11:14", "10:11:15", "10:11:21", "10:10:1", "10:10:2")
)


class UFeederCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry, api):
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(seconds=POLL_SECONDS),
        )
        self.api = api
        self.transport = UFeederTransport(
            api, on_report=self._report, on_connection_change=self._connection
        )
        self._locks = defaultdict(asyncio.Lock)
        self._shadow_task = None
        self._closing = False
        self._history_cache = {}
        self._history_last_fetch = {}
        self._history_day = {}

    async def _async_update_data(self):
        try:
            devices = await self.api.async_devices()
            # Polls are intentionally sequential to bound account request load.
            result = {}
            for device in devices:
                await self.api.async_device_state(device)
                today = dt_util.now().date()
                if self._history_day.get(device.key) != today:
                    self._history_cache.pop(device.key, None)
                    self._history_last_fetch.pop(device.key, None)
                if (
                    time.monotonic() - self._history_last_fetch.get(device.key, -300)
                    >= 300
                ):
                    self._history_last_fetch[device.key] = time.monotonic()
                    self._history_day[device.key] = today
                    try:
                        self._history_cache[device.key] = await async_fetch_history(
                            self.api, device
                        )
                        self._history_day[device.key] = today
                    except InvalidAuth:
                        raise
                    except UFeederError:
                        # History permissions/availability must not hide controls.
                        _LOGGER.debug("U Feeder history is unavailable")
                device.history = self._history_cache.get(device.key, {})
                old = (self.data or {}).get(device.key)
                if old:
                    for path, timestamp in old.timestamps.items():
                        if timestamp > device.timestamps.get(path, 0):
                            device.properties[path] = deepcopy(old.properties[path])
                            device.timestamps[path] = timestamp
                result[device.key] = device
            await self.transport.async_set_devices(
                [device.device_id for device in result.values()]
            )
            return result
        except InvalidAuth as err:
            raise ConfigEntryAuthFailed(
                "Cloud session ended. Use a dedicated shared account for HA."
            ) from err
        except UFeederError as err:
            raise UpdateFailed(str(err)) from err

    def _connection(self, connected):
        if self.data is not None and not self._closing:
            self.async_update_listeners()

    def _report(self, device_id, report):
        if not self.data or self._closing:
            return
        device = next(
            (device for device in self.data.values() if device.device_id == device_id),
            None,
        )
        if device is None:
            return
        device.online = True
        needs_shadow = bool(report.events)
        if report.events:
            self._history_last_fetch[device.key] = -300
        for path, raw in report.attributes.items():
            if report.timestamp < device.timestamps.get(path, 0):
                continue
            if path in STRUCT_PROPERTIES:
                needs_shadow = True
                continue
            try:
                value = decode_scalar(
                    raw,
                    boolean=path in BOOL_PROPERTIES,
                    signed=path
                    in ("10:11:11", "10:11:12", "10:11:19", "10:11:20", "10:10:10"),
                )
            except ProtocolError:
                continue
            device.properties[path] = value
            device.timestamps[path] = report.timestamp
        # Frequent motion reports must not postpone REST polling indefinitely.
        self.async_update_listeners()
        if needs_shadow and (self._shadow_task is None or self._shadow_task.done()):
            self._shadow_task = self.hass.async_create_background_task(
                self._delayed_refresh(), "U Feeder shadow refresh"
            )

    async def _delayed_refresh(self):
        await asyncio.sleep(2)
        await self.async_request_refresh()

    async def async_shutdown(self):
        if self._closing:
            return
        self._closing = True
        if self._shadow_task and not self._shadow_task.done():
            self._shadow_task.cancel()
            await asyncio.gather(self._shadow_task, return_exceptions=True)
        await self.transport.async_stop()
        await super().async_shutdown()

    def _check_writable(self, key, *, command=False, device=None):
        device = device or (self.data or {}).get(key)
        if not self.last_update_success or not device or not device.online:
            raise ServiceValidationError("The feeder is offline")
        if not self.transport.connected:
            raise ServiceValidationError("The cloud control connection is unavailable")
        state = device.properties.get("10:11:1")
        if state in (4, "GLOBAL_STATE_UPDATING"):
            raise ServiceValidationError("The feeder is updating its firmware")
        if command and state in (2, "GLOBAL_STATE_PAIRING"):
            raise ServiceValidationError("The feeder is in pairing mode")
        if device.properties.get("10:10:7") in (0, "POWER_SUPPLY_BATTERY"):
            raise ServiceValidationError(
                "Connect the feeder to mains power to change settings"
            )
        return device

    async def async_invoke(self, key, path, argument=b""):
        async with self._locks[key]:
            device = self._check_writable(key, command=True)
            try:
                await self.transport.async_invoke(device.device_id, path, argument)
            except UFeederError as err:
                raise HomeAssistantError(str(err)) from err
        await self.async_request_refresh()

    async def async_write(self, key, path, payload):
        async with self._locks[key]:
            device = self._check_writable(key)
            try:
                await self.transport.async_write(device.device_id, path, payload)
            except UFeederError as err:
                raise HomeAssistantError(str(err)) from err
        await self.async_request_refresh()

    async def async_preferences(self, key, **changes):
        from .controls import encode_preferences

        async with self._locks[key]:
            device = self._check_writable(key)
            try:
                # Read/merge/write to preserve settings changed in the mobile app.
                device = await self.api.async_device_state(deepcopy(device))
                self._check_writable(key, device=device)
                payload = encode_preferences(
                    device.properties.get("10:11:7", {}), **changes
                )
                await self.transport.async_write(device.device_id, (10, 11, 7), payload)
            except (UFeederError, ValueError) as err:
                raise HomeAssistantError(str(err)) from err
        await self.async_request_refresh()

    async def async_save_schedule(self, key, schedule):
        from .schedules import (
            decode_schedules,
            encode_schedules,
            validate_schedule_change,
        )

        async with self._locks[key]:
            device = self._check_writable(key, command=True)
            try:
                device = await self.api.async_device_state(deepcopy(device))
                self._check_writable(key, command=True, device=device)
                current = decode_schedules(device.properties.get("10:11:13"))
                executing = decode_schedules(device.properties.get("10:11:14"))
                if schedule.id and any(item.id == schedule.id for item in executing):
                    raise ServiceValidationError(
                        "An executing schedule cannot be edited"
                    )
                count = device.properties.get("10:11:5")
                if count not in (3, 4):
                    raise ServiceValidationError("The tray layout is unknown")
                validate_schedule_change(schedule, current, tray_count=count)
                await self.transport.async_invoke(
                    device.device_id,
                    (10, 11, 9 if schedule.id else 7),
                    encode_schedules([schedule]),
                )
            except UFeederError as err:
                raise HomeAssistantError(str(err)) from err
        await self.async_request_refresh()

    async def async_delete_schedule(self, key, schedule_id):
        from .schedules import decode_schedules, encode_remove_schedules

        async with self._locks[key]:
            device = self._check_writable(key, command=True)
            try:
                device = await self.api.async_device_state(deepcopy(device))
                self._check_writable(key, command=True, device=device)
                current = decode_schedules(device.properties.get("10:11:13"))
                if not any(item.id == schedule_id for item in current):
                    raise ServiceValidationError("The schedule no longer exists")
                await self.transport.async_invoke(
                    device.device_id,
                    (10, 11, 8),
                    encode_remove_schedules([schedule_id]),
                )
            except (UFeederError, ValueError) as err:
                raise HomeAssistantError(str(err)) from err
        await self.async_request_refresh()
