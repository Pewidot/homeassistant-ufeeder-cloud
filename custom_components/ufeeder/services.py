"""UI-friendly feeding action for Home Assistant automations and scripts."""

from homeassistant.const import ATTR_DEVICE_ID
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .controls import DURATION_MINUTES, encode_feed_options
from .schema import vol


def _whole_number(value):
    """Accept selector numbers without rounding or interpreting booleans."""
    if isinstance(value, bool):
        raise vol.Invalid("Expected a whole number")
    try:
        integer = int(value)
    except (ValueError, TypeError, OverflowError) as err:
        raise vol.Invalid("Expected a whole number") from err
    if not isinstance(value, (int, float, str)) or (
        not isinstance(value, str) and value != integer
    ):
        raise vol.Invalid("Expected a whole number")
    return integer


FEED_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): str,
        vol.Required("compartment"): vol.All(_whole_number, vol.Range(min=1, max=4)),
        vol.Optional("duration_minutes", default=3): vol.All(
            _whole_number, vol.In(DURATION_MINUTES)
        ),
        vol.Optional("preheat", default=False): bool,
        vol.Optional("temperature", default=24): vol.All(
            _whole_number, vol.In((20, 24, 28))
        ),
    }
)


def async_register_services(hass):
    if hass.services.has_service(DOMAIN, "feed"):
        return

    async def feed(call):
        device = dr.async_get(hass).async_get(call.data[ATTR_DEVICE_ID])
        if device is None:
            raise ServiceValidationError("Choose a U Feeder device")
        keys = {key for domain, key in device.identifiers if domain == DOMAIN}
        # Newer HA versions scope the same feeder identifier to each account's
        # device entry. Route through the account the user actually selected.
        entry_id = getattr(device, "config_entry_id", None)
        account_entries = {entry_id} if entry_id else device.config_entries
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.entry_id not in account_entries:
                continue
            coordinator = getattr(entry, "runtime_data", None)
            if coordinator is None:
                continue
            key = next((key for key in keys if key in (coordinator.data or {})), None)
            if key is None:
                continue
            feeder = coordinator._check_writable(key, command=True)
            try:
                payload = encode_feed_options(
                    tray=call.data["compartment"],
                    compartment_count=feeder.properties.get("10:11:5"),
                    duration_seconds=call.data["duration_minutes"] * 60,
                    preheat=call.data["preheat"],
                    temperature=call.data["temperature"],
                    user_id=coordinator.api.user_id,
                )
            except ValueError as err:
                raise ServiceValidationError(str(err)) from err
            await coordinator.async_invoke(key, (10, 11, 2), payload)
            return
        raise ServiceValidationError("The selected feeder is not loaded")

    hass.services.async_register(DOMAIN, "feed", feed, schema=FEED_SCHEMA)
