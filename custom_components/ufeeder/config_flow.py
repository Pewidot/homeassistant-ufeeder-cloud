"""UI login and explicit reauthentication; no automatic password relogin."""

from __future__ import annotations

from typing import Any
import uuid

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import selector

from .api import ApiError, CannotConnect, InvalidAuth, InvalidRegion, UFeederApi
from .const import CONF_CLIENT_ID, CONF_REGION, CONF_SESSION, DOMAIN
from .schema import secret, vol


def login_schema(values: dict | None = None) -> vol.Schema:
    values = values or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_USERNAME, default=values.get(CONF_USERNAME, "")
            ): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.EMAIL)
            ),
            vol.Required(secret(CONF_PASSWORD)): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
            vol.Required(
                CONF_REGION, default=values.get(CONF_REGION, "DE")
            ): selector.CountrySelector(),
        }
    )


class UFeederConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up a dedicated U Feeder cloud account."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        from .options_flow import UFeederOptionsFlow

        return UFeederOptionsFlow()

    async def _credentials(
        self, user_input: dict[str, Any] | None, step_id: str
    ) -> ConfigFlowResult:
        errors = {}
        entry = None
        if step_id == "reauth_confirm":
            entry = self._get_reauth_entry()
        elif step_id == "reconfigure":
            entry = self._get_reconfigure_entry()
        if user_input is not None:
            client_id = entry.data[CONF_CLIENT_ID] if entry else uuid.uuid4().hex
            api = UFeederApi(
                async_get_clientsession(self.hass),
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
                user_input[CONF_REGION],
                client_id,
            )
            try:
                await api.async_login()
                devices = await api.async_devices()
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except InvalidRegion:
                errors[CONF_REGION] = "invalid_region"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except ApiError:
                errors["base"] = "cloud_error"
            else:
                if not devices:
                    errors["base"] = "no_devices"
                else:
                    await self.async_set_unique_id(api.unique_id)
                    data = {
                        **user_input,
                        CONF_USERNAME: api.username,
                        CONF_REGION: api.region,
                        CONF_CLIENT_ID: client_id,
                        CONF_SESSION: api.export_session(),
                    }
                    if entry is not None:
                        self._abort_if_unique_id_mismatch(reason="wrong_account")
                        return self.async_update_reload_and_abort(
                            entry, data_updates=data
                        )
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=f"U Feeder ({api.region})", data=data
                    )
        return self.async_show_form(
            step_id=step_id,
            data_schema=login_schema(
                user_input or (dict(entry.data) if entry else None)
            ),
            errors=errors,
        )

    async def async_step_user(self, user_input=None):
        return await self._credentials(user_input, "user")

    async def async_step_reauth(self, entry_data):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        return await self._credentials(user_input, "reauth_confirm")

    async def async_step_reconfigure(self, user_input=None):
        return await self._credentials(user_input, "reconfigure")
