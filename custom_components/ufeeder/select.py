"""Discrete settings use the same allowed values as the app."""

from dataclasses import dataclass

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity import EntityCategory

from .controls import DURATION_MINUTES, encode_scalar_property
from .entity import UFeederEntity, setup_entities


@dataclass(frozen=True, kw_only=True)
class FeederSelectDescription(SelectEntityDescription):
    path: str
    preference: str | None = None


SELECTS = (
    FeederSelectDescription(
        key="cooling_temperature",
        translation_key="cooling_temperature",
        path="10:11:12",
        options=["14", "17", "20"],
        icon="mdi:snowflake-thermometer",
    ),
    FeederSelectDescription(
        key="heating_temperature",
        translation_key="heating_temperature",
        path="10:11:7",
        preference="expectedHeatingTemperature",
        options=["20", "24", "28"],
        icon="mdi:thermometer",
    ),
    FeederSelectDescription(
        key="feeding_duration",
        translation_key="feeding_duration",
        path="10:11:7",
        preference="feedDurationSeconds",
        options=[str(n) for n in DURATION_MINUTES],
        icon="mdi:timer-outline",
        entity_category=EntityCategory.CONFIG,
    ),
)


async def async_setup_entry(hass, entry, async_add_entities):
    setup_entities(
        entry,
        async_add_entities,
        lambda coordinator, key: [
            UFeederSelect(coordinator, key, desc) for desc in SELECTS
        ],
    )


class UFeederSelect(UFeederEntity, SelectEntity):
    control = True

    @property
    def current_option(self):
        description = self.entity_description
        value = self.value(description.path)
        if description.preference:
            value = (
                value.get(description.preference) if isinstance(value, dict) else None
            )
        if description.key == "feeding_duration":
            value = value // 60 if isinstance(value, int) and not value % 60 else None
        result = str(value)
        return result if result in description.options else None

    async def async_select_option(self, option):
        if option not in self.entity_description.options:
            raise ServiceValidationError("Unsupported setting")
        if self.entity_description.key == "feeding_duration":
            await self.coordinator.async_preferences(
                self.device_key, duration_minutes=int(option)
            )
        elif self.entity_description.key == "heating_temperature":
            await self.coordinator.async_preferences(
                self.device_key, heating_temperature=int(option)
            )
        else:
            path = (10, 11, 12)
            await self.coordinator.async_write(
                self.device_key, path, encode_scalar_property(path, int(option))
            )
