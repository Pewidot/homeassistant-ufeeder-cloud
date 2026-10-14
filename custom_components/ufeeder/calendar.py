"""Calendar view of active weekly plans stored on the feeder."""

from datetime import timedelta

from homeassistant.components.calendar import (
    CalendarEntity,
    CalendarEntityDescription,
    CalendarEvent,
)
from homeassistant.util import dt as dt_util

from .entity import UFeederEntity, setup_entities
from .schedules import decode_schedules, next_occurrences


async def async_setup_entry(hass, entry, async_add_entities):
    setup_entities(
        entry,
        async_add_entities,
        lambda coordinator, key: [
            UFeederCalendar(
                coordinator,
                key,
                CalendarEntityDescription(
                    key="feeding_schedule", translation_key="feeding_schedule"
                ),
            )
        ],
    )


class UFeederCalendar(UFeederEntity, CalendarEntity):
    def _events(self, start, end):
        if self.value("10:11:17") is not True:
            return []
        result = []
        for schedule in decode_schedules(self.value("10:11:13")):
            for beginning, ending in next_occurrences(schedule, start, end):
                result.append(
                    CalendarEvent(
                        start=beginning,
                        end=ending,
                        summary=f"{self.device.name} · #{schedule.id} · {schedule.tray_compartment}",
                        uid=str(schedule.id),
                        recurrence_id=beginning.isoformat(),
                        description=f"{schedule.duration_seconds // 60} min",
                    )
                )
        return sorted(result, key=lambda event: event.start)

    @property
    def event(self):
        now = dt_util.utcnow()
        events = self._events(now, now + timedelta(days=8))
        return events[0] if events else None

    async def async_get_events(self, hass, start_date, end_date):
        return self._events(start_date, end_date)

    @property
    def extra_state_attributes(self):
        return {
            "automatic_feeding": self.value("10:11:17"),
            "schedule_count": len(decode_schedules(self.value("10:11:13"))),
            "editing": "integration_options",
        }
