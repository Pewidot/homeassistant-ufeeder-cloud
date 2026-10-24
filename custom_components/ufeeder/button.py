"""Explicit feeding buttons; no command replay or optimistic success."""

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.exceptions import ServiceValidationError

from .controls import COMMAND_PATHS, encode_feed_options
from .entity import UFeederEntity, setup_entities


DESCRIPTIONS = (
    ButtonEntityDescription(
        key="feed_now", translation_key="feed_now", icon="mdi:food"
    ),
    ButtonEntityDescription(
        key="stop_feeding",
        translation_key="stop_feeding",
        icon="mdi:stop-circle-outline",
    ),
    ButtonEntityDescription(
        key="skip_heating",
        translation_key="skip_heating",
        icon="mdi:fast-forward-outline",
    ),
    ButtonEntityDescription(
        key="check_firmware", translation_key="check_firmware", icon="mdi:update"
    ),
)


async def async_setup_entry(hass, entry, async_add_entities):
    setup_entities(
        entry,
        async_add_entities,
        lambda coordinator, key: [
            UFeederButton(coordinator, key, desc) for desc in DESCRIPTIONS
        ],
    )


class UFeederButton(UFeederEntity, ButtonEntity):
    control = True

    async def async_press(self):
        key = self.entity_description.key
        if key == "feed_now":
            prefs = self.value("10:11:7")
            preheat = self.value("10:11:22")
            if not isinstance(prefs, dict) or not isinstance(preheat, bool):
                raise ServiceValidationError("Feeding settings are not yet available")
            try:
                payload = encode_feed_options(
                    tray=self.value("10:11:6"),
                    compartment_count=self.value("10:11:5"),
                    duration_seconds=prefs.get("feedDurationSeconds"),
                    preheat=preheat,
                    temperature=24,
                    user_id=self.coordinator.api.user_id,
                )
            except ValueError as err:
                raise ServiceValidationError(str(err)) from err
            await self.coordinator.async_invoke(self.device_key, (10, 11, 2), payload)
        elif key == "check_firmware":
            await self.coordinator.async_invoke(
                self.device_key, COMMAND_PATHS["detect_firmware_update"]
            )
        else:
            await self.coordinator.async_invoke(self.device_key, COMMAND_PATHS[key])
