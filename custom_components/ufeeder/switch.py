"""Device switches with observed state and acknowledged writes."""

from dataclasses import dataclass

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.helpers.entity import EntityCategory

from .controls import encode_scalar_property
from .entity import UFeederEntity, setup_entities


@dataclass(frozen=True, kw_only=True)
class FeederSwitchDescription(SwitchEntityDescription):
    path: str
    preference: str | None = None


SWITCHES = (
    FeederSwitchDescription(
        key="pet_detection",
        translation_key="pet_detection",
        path="10:11:8",
        icon="mdi:cat",
    ),
    FeederSwitchDescription(
        key="temperature_control",
        translation_key="temperature_control",
        path="10:11:10",
        icon="mdi:snowflake",
    ),
    FeederSwitchDescription(
        key="auto_feeding",
        translation_key="auto_feeding",
        path="10:11:17",
        icon="mdi:calendar-clock",
    ),
    FeederSwitchDescription(
        key="heat_before_feed_now",
        translation_key="heat_before_feed_now",
        path="10:11:22",
        icon="mdi:heat-wave",
    ),
    FeederSwitchDescription(
        key="heat_before_feeding",
        translation_key="heat_before_feeding",
        path="10:11:7",
        preference="heatBeforeFeeding",
        icon="mdi:heat-wave",
    ),
    FeederSwitchDescription(
        key="automatic_update",
        translation_key="automatic_update",
        path="10:10:5",
        icon="mdi:update",
        entity_category=EntityCategory.CONFIG,
    ),
)


async def async_setup_entry(hass, entry, async_add_entities):
    setup_entities(
        entry,
        async_add_entities,
        lambda coordinator, key: [
            UFeederSwitch(coordinator, key, desc) for desc in SWITCHES
        ],
    )


class UFeederSwitch(UFeederEntity, SwitchEntity):
    control = True

    @property
    def is_on(self):
        value = self.value(self.entity_description.path)
        if self.entity_description.preference:
            value = (
                value.get(self.entity_description.preference)
                if isinstance(value, dict)
                else None
            )
        return value if isinstance(value, bool) else None

    async def _set(self, value):
        if self.entity_description.preference:
            await self.coordinator.async_preferences(
                self.device_key, heat_before_feeding=value
            )
        else:
            path = tuple(map(int, self.entity_description.path.split(":")))
            await self.coordinator.async_write(
                self.device_key, path, encode_scalar_property(path, value)
            )

    async def async_turn_on(self, **kwargs):
        await self._set(True)

    async def async_turn_off(self, **kwargs):
        await self._set(False)
