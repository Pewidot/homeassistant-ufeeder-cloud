"""Native HA forms for manual feeding and device-resident weekly schedules."""

from dataclasses import replace
from datetime import time

from homeassistant.config_entries import OptionsFlow
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector
from homeassistant.util import dt as dt_util

from .controls import DURATION_MINUTES, encode_feed_options
from .schedules import FeedingSchedule, decode_schedules, local_to_utc, utc_to_local
from .schema import vol


def select(options, **kwargs):
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=options, mode=selector.SelectSelectorMode.DROPDOWN, **kwargs
        )
    )


def duration_selector():
    return select(
        [{"value": str(value), "label": f"{value} min"} for value in DURATION_MINUTES]
    )


class UFeederOptionsFlow(OptionsFlow):
    """Edits are applied by acknowledged device transactions, not stored as HA timers."""

    _device_key = None
    _selected = None
    _action = None

    @property
    def coordinator(self):
        return self.config_entry.runtime_data

    @property
    def device(self):
        return self.coordinator.data[self._device_key]

    @property
    def schedules(self):
        return decode_schedules(self.device.properties.get("10:11:13"))

    @property
    def offset(self):
        return int(dt_util.now().utcoffset().total_seconds() // 60)

    def _done(self):
        return self.async_create_entry(title="", data=dict(self.config_entry.options))

    async def async_step_init(self, user_input=None):
        coordinator = getattr(self.config_entry, "runtime_data", None)
        if coordinator is None or not coordinator.data:
            return self.async_abort(reason="not_ready")
        if user_input is not None:
            key = user_input["device"]
            if key not in coordinator.data:
                return self.async_abort(reason="not_ready")
            self._device_key = key
            return await self.async_step_manage()
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required("device"): select(
                        [
                            {"value": key, "label": device.name}
                            for key, device in coordinator.data.items()
                        ]
                    )
                }
            ),
        )

    async def async_step_manage(self, user_input=None):
        return self.async_show_menu(
            step_id="manage",
            menu_options=[
                "feed_now",
                "add_schedule",
                "edit_schedule",
                "delete_schedule",
            ],
            description_placeholders={"device": self.device.name},
        )

    def _feeding_fields(self, values=None):
        values = values or {}
        count = self.device.properties.get("10:11:5")
        if count not in (3, 4):
            raise ValueError("Tray layout is unknown")
        return {
            vol.Required("tray", default=str(values.get("tray", 1))): select(
                [str(n) for n in range(1, count + 1)]
            ),
            vol.Required(
                "duration_minutes", default=str(values.get("duration_minutes", 3))
            ): duration_selector(),
            vol.Required(
                "preheat", default=values.get("preheat", False)
            ): selector.BooleanSelector(),
            vol.Required(
                "temperature", default=str(values.get("temperature", 24))
            ): select(["20", "24", "28"]),
        }

    async def async_step_feed_now(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                payload = encode_feed_options(
                    tray=int(user_input["tray"]),
                    compartment_count=self.device.properties.get("10:11:5"),
                    duration_seconds=int(user_input["duration_minutes"]) * 60,
                    preheat=user_input["preheat"],
                    temperature=int(user_input["temperature"]),
                    user_id=self.coordinator.api.user_id,
                )
                await self.coordinator.async_invoke(
                    self._device_key, (10, 11, 2), payload
                )
            except (ValueError, HomeAssistantError):
                errors["base"] = "command_failed"
            else:
                return self._done()
        try:
            fields = self._feeding_fields(user_input)
        except ValueError:
            return self.async_abort(reason="tray_unknown")
        return self.async_show_form(
            step_id="feed_now", data_schema=vol.Schema(fields), errors=errors
        )

    async def async_step_add_schedule(self, user_input=None):
        self._selected = None
        return await self.async_step_schedule(user_input)

    async def async_step_edit_schedule(self, user_input=None):
        self._action = "edit"
        return await self.async_step_choose_schedule(user_input)

    async def async_step_delete_schedule(self, user_input=None):
        self._action = "delete"
        return await self.async_step_choose_schedule(user_input)

    async def async_step_choose_schedule(self, user_input=None):
        if not self.schedules:
            return self.async_abort(reason="no_schedules")
        if user_input is not None:
            self._selected = next(
                (
                    item
                    for item in self.schedules
                    if str(item.id) == user_input["schedule"]
                ),
                None,
            )
            if self._selected is None:
                return self.async_abort(reason="schedule_changed")
            if self._action == "delete":
                return await self.async_step_confirm_delete()
            return await self.async_step_schedule()
        options = []
        for item in self.schedules:
            hour, minute, days = utc_to_local(
                item.hour, item.minute, item.weekdays, self.offset
            )
            options.append(
                {
                    "value": str(item.id),
                    "label": f"#{item.id} · {hour:02}:{minute:02} · {','.join(str(day) for day in days)} · {item.tray_compartment}",
                }
            )
        return self.async_show_form(
            step_id="choose_schedule",
            data_schema=vol.Schema({vol.Required("schedule"): select(options)}),
        )

    async def async_step_confirm_delete(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                await self.coordinator.async_delete_schedule(
                    self._device_key, self._selected.id
                )
            except HomeAssistantError:
                errors["base"] = "command_failed"
            else:
                return self._done()
        return self.async_show_form(
            step_id="confirm_delete",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={"schedule": str(self._selected.id)},
        )

    async def async_step_schedule(self, user_input=None):
        errors = {}
        defaults = {}
        if self._selected:
            item = self._selected
            hour, minute, days = utc_to_local(
                item.hour, item.minute, item.weekdays, self.offset
            )
            defaults = {
                "time": f"{hour:02}:{minute:02}:00",
                "weekdays": [str(day) for day in days],
                "enabled": item.enabled,
                "tray": item.tray_compartment,
                "duration_minutes": item.duration_seconds // 60,
                "preheat": bool(item.heat_before_feeding),
                "temperature": item.heating_temperature or 24,
            }
        if user_input is not None:
            defaults.update(user_input)
            try:
                start = time.fromisoformat(user_input["time"])
                if start.second or start.microsecond:
                    raise ValueError("Schedule time must use whole minutes")
                hour, minute, mask = local_to_utc(
                    start.hour,
                    start.minute,
                    [int(day) for day in user_input["weekdays"]],
                    self.offset,
                )
                schedule = FeedingSchedule(
                    id=self._selected.id if self._selected else 0,
                    hour=hour,
                    minute=minute,
                    weekdays=mask,
                    enabled=user_input["enabled"],
                    tray_compartment=int(user_input["tray"]),
                    duration_seconds=int(user_input["duration_minutes"]) * 60,
                    heat_before_feeding=self._selected.heat_before_feeding
                    if self._selected
                    else None,
                    heating_temperature=self._selected.heating_temperature
                    if self._selected
                    else None,
                    operator=self._selected.operator if self._selected else None,
                )
                # Preserve future protobuf fields when editing an existing plan.
                if self._selected:
                    schedule = replace(
                        self._selected,
                        **{
                            field: getattr(schedule, field)
                            for field in (
                                "hour",
                                "minute",
                                "weekdays",
                                "enabled",
                                "tray_compartment",
                                "duration_seconds",
                                "heat_before_feeding",
                                "heating_temperature",
                                "operator",
                            )
                        },
                    )
                await self.coordinator.async_save_schedule(self._device_key, schedule)
            except ValueError:
                errors["base"] = "invalid_schedule"
            except HomeAssistantError:
                errors["base"] = "command_failed"
            else:
                return self._done()
        try:
            fields = self._feeding_fields(defaults)
            fields = {
                key: value
                for key, value in fields.items()
                if str(key) not in ("preheat", "temperature")
            }
        except ValueError:
            return self.async_abort(reason="tray_unknown")
        fields = {
            vol.Required(
                "time", default=defaults.get("time", "08:00:00")
            ): selector.TimeSelector(),
            vol.Required(
                "weekdays",
                default=defaults.get("weekdays", [str(day) for day in range(1, 8)]),
            ): select(
                ["1", "2", "3", "4", "5", "6", "7"],
                multiple=True,
                translation_key="weekdays",
            ),
            vol.Required(
                "enabled", default=defaults.get("enabled", True)
            ): selector.BooleanSelector(),
            **fields,
        }
        return self.async_show_form(
            step_id="schedule", data_schema=vol.Schema(fields), errors=errors
        )
