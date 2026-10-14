"""U Feeder cloud integration."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import UFeederApi
from .const import CONF_CLIENT_ID, CONF_REGION, CONF_SESSION
from .coordinator import UFeederCoordinator
from .services import async_register_services

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.SELECT,
    Platform.BUTTON,
    Platform.CALENDAR,
    Platform.UPDATE,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    def save_session(session):
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_SESSION: session}
        )

    api = UFeederApi(
        async_get_clientsession(hass),
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        entry.data[CONF_REGION],
        entry.data[CONF_CLIENT_ID],
        entry.data.get(CONF_SESSION),
        save_session,
    )
    coordinator = UFeederCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await coordinator.transport.async_start(
        [device.device_id for device in coordinator.data.values()]
    )
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await coordinator.async_shutdown()
        raise
    async_register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.async_shutdown()
        return True
    return False
