"""Feeder temperature, tray, power and operating state sensors."""

from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfTemperature, UnitOfTime
from homeassistant.helpers.entity import EntityCategory

from .entity import UFeederEntity, setup_entities
from .history import STATUS_NAMES, SOURCE_NAMES


@dataclass(frozen=True, kw_only=True)
class FeederSensorDescription(SensorEntityDescription):
    path: str
    prefix: str = ""


SENSORS = (
    FeederSensorDescription(
        key="feed_count_today",
        translation_key="feed_count_today",
        path="history:feed_count_today",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:counter",
    ),
    FeederSensorDescription(
        key="last_feeding",
        translation_key="last_feeding",
        path="history:last_feeding",
        device_class=SensorDeviceClass.TIMESTAMP,
    ),
    FeederSensorDescription(
        key="last_feeding_status",
        translation_key="last_feeding_status",
        path="history:last_status",
        device_class=SensorDeviceClass.ENUM,
        options=[*STATUS_NAMES, "unknown"],
    ),
    FeederSensorDescription(
        key="last_feeding_source",
        translation_key="last_feeding_source",
        path="history:last_source",
        device_class=SensorDeviceClass.ENUM,
        options=[*SOURCE_NAMES, "unknown"],
    ),
    FeederSensorDescription(
        key="temperature",
        translation_key="temperature",
        path="10:11:11",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    FeederSensorDescription(
        key="tray_temperature",
        translation_key="tray_temperature",
        path="10:11:20",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
    ),
    FeederSensorDescription(
        key="thermoelectric_temperature",
        translation_key="thermoelectric_temperature",
        path="10:11:19",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    FeederSensorDescription(
        key="global_state",
        translation_key="global_state",
        path="10:11:1",
        prefix="GLOBAL_STATE_",
        device_class=SensorDeviceClass.ENUM,
        options=[
            "resetting",
            "idle",
            "pairing",
            "feeding",
            "updating",
            "testing",
            "erasing",
        ],
    ),
    FeederSensorDescription(
        key="feed_state",
        translation_key="feed_state",
        path="10:11:2",
        prefix="FEED_STATE_",
        device_class=SensorDeviceClass.ENUM,
        options=["stopped", "preparing", "starting", "serving", "stopping", "fault"],
    ),
    FeederSensorDescription(
        key="door_state",
        translation_key="door_state",
        path="10:11:3",
        prefix="DOOR_STATE_",
        device_class=SensorDeviceClass.ENUM,
        options=["closed", "opening", "opened", "closing", "fault"],
    ),
    FeederSensorDescription(
        key="tray_state",
        translation_key="tray_state",
        path="10:11:4",
        prefix="TRAY_STATE_",
        device_class=SensorDeviceClass.ENUM,
        options=["no_tray", "aligned", "misaligned", "rotating", "fault"],
    ),
    FeederSensorDescription(
        key="compartments",
        translation_key="compartments",
        path="10:11:5",
        icon="mdi:circle-slice-4",
    ),
    FeederSensorDescription(
        key="feedable_compartment",
        translation_key="feedable_compartment",
        path="10:11:6",
        icon="mdi:food",
    ),
    FeederSensorDescription(
        key="temperature_trend",
        translation_key="temperature_trend",
        path="10:11:18",
        prefix="TEMPERATURE_TREND_",
        device_class=SensorDeviceClass.ENUM,
        options=["stabilizing", "cooling_down", "heating_up"],
    ),
    FeederSensorDescription(
        key="temperature_fault",
        translation_key="temperature_fault",
        path="10:11:16",
        prefix="TEMPERATURE_SENSOR_FAULT_",
        device_class=SensorDeviceClass.ENUM,
        options=["none", "open_circuit", "short_circuit"],
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    FeederSensorDescription(
        key="power_supply",
        translation_key="power_supply",
        path="10:10:7",
        prefix="POWER_SUPPLY_",
        device_class=SensorDeviceClass.ENUM,
        options=["battery", "dc"],
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    FeederSensorDescription(
        key="battery",
        translation_key="battery",
        path="10:10:8",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    FeederSensorDescription(
        key="preheat_duration",
        translation_key="preheat_duration",
        path="10:11:23",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
    ),
)


async def async_setup_entry(hass, entry, async_add_entities):
    setup_entities(
        entry,
        async_add_entities,
        lambda coordinator, key: [
            UFeederSensor(coordinator, key, desc) for desc in SENSORS
        ],
    )


class UFeederSensor(UFeederEntity, SensorEntity):
    @property
    def native_value(self):
        description = self.entity_description
        if description.path.startswith("history:"):
            return (
                self.device.history.get(description.path.split(":", 1)[1])
                if self.device
                else None
            )
        value = self.value(description.path)
        if description.options:
            if isinstance(value, int) and not isinstance(value, bool):
                return (
                    description.options[value]
                    if 0 <= value < len(description.options)
                    else None
                )
            if isinstance(value, str):
                value = value.removeprefix(description.prefix).lower()
                return value if value in description.options else None
            return None
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        if description.key == "battery" and not 0 < value <= 100:
            return None
        return value

    @property
    def extra_state_attributes(self):
        if self.entity_description.key != "last_feeding" or not self.device:
            return None
        history = self.device.history
        return {
            key: history.get(key)
            for key in (
                "last_end",
                "last_compartment",
                "recent_records",
                "history_truncated",
                "history_since",
                "fetched_at",
            )
        }
