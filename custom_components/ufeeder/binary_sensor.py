"""Connectivity and pet detection."""

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.helpers.entity import EntityCategory

from .entity import UFeederEntity, setup_entities


DESCRIPTIONS = (
    BinarySensorEntityDescription(
        key="online",
        translation_key="online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BinarySensorEntityDescription(
        key="pet_detected",
        translation_key="pet_detected",
        device_class=BinarySensorDeviceClass.OCCUPANCY,
    ),
)


async def async_setup_entry(hass, entry, async_add_entities):
    setup_entities(
        entry,
        async_add_entities,
        lambda coordinator, key: [
            UFeederBinarySensor(coordinator, key, desc) for desc in DESCRIPTIONS
        ],
    )


class UFeederBinarySensor(UFeederEntity, BinarySensorEntity):
    @property
    def available(self):
        if self.entity_description.key == "online":
            return self.coordinator.last_update_success and self.device is not None
        return super().available

    @property
    def is_on(self):
        if self.entity_description.key == "online":
            return self.device.online if self.device else None
        value = self.value("10:11:9")
        return value if isinstance(value, bool) else None
