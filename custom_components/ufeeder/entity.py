"""Shared device identity, availability and dynamic discovery."""

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


class UFeederEntity(CoordinatorEntity):
    _attr_has_entity_name = True
    control = False

    def __init__(self, coordinator, device_key: str, description) -> None:
        super().__init__(coordinator)
        self.device_key = device_key
        self.entity_description = description
        self._attr_unique_id = f"{device_key}:{description.key}"

    @property
    def device(self):
        return self.coordinator.data.get(self.device_key)

    @property
    def device_info(self):
        device = self.device
        firmware = device.properties.get("10:10:1", {}) if device else {}
        return DeviceInfo(
            identifiers={(DOMAIN, self.device_key)},
            name=device.name if device else "U Feeder",
            manufacturer="UBTECH",
            model="U Feeder",
            sw_version=firmware.get("name") if isinstance(firmware, dict) else None,
        )

    @property
    def available(self):
        return bool(
            super().available
            and self.device
            and self.device.online
            and (not self.control or self.coordinator.transport.connected)
        )

    def value(self, path: str):
        return self.device.properties.get(path) if self.device else None


def setup_entities(entry, async_add_entities, factory):
    """Include newly discovered/shared feeders without restarting Home Assistant."""
    coordinator = entry.runtime_data
    known = set()

    def discover():
        entities = []
        for key in coordinator.data:
            if key not in known:
                entities.extend(factory(coordinator, key))
                known.add(key)
        if entities:
            async_add_entities(entities)

    discover()
    entry.async_on_unload(coordinator.async_add_listener(discover))
