"""Config flow for REXLiTE."""

from __future__ import annotations

import re
from typing import Any, Final

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .authentication import (
    CannotConnectError,
    InvalidAuthError,
    async_validate_gateway_credentials,
)
from .const import (
    CONF_AGENT_AUTH_TOKEN,
    CONF_AGENT_ID,
    CONF_GATEWAY_WS_URL,
    CONF_HOME_ASSISTANT_URL,
    CONF_REMOTE_ADMIN_ENABLED,
    DEFAULT_GATEWAY_WS_URL,
    DEFAULT_HOME_ASSISTANT_URL,
    DEFAULT_REMOTE_ADMIN_ENABLED,
    DOMAIN,
)
from .protocol import (
    ProtocolError,
    validate_gateway_url,
    validate_local_url,
)

_AGENT_ID_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    values = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_AGENT_ID, default=values.get(CONF_AGENT_ID, "")
            ): TextSelector(),
            vol.Required(CONF_AGENT_AUTH_TOKEN): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
            vol.Required(
                CONF_REMOTE_ADMIN_ENABLED,
                default=values.get(
                    CONF_REMOTE_ADMIN_ENABLED, DEFAULT_REMOTE_ADMIN_ENABLED
                ),
            ): BooleanSelector(),
        }
    )


async def _validate_input(hass: Any, user_input: dict[str, Any]) -> dict[str, Any]:
    agent_id = str(user_input[CONF_AGENT_ID]).strip()
    token = str(user_input[CONF_AGENT_AUTH_TOKEN]).strip()
    if not _AGENT_ID_PATTERN.fullmatch(agent_id):
        raise ProtocolError("invalid agent id")
    if not token or len(token) > 4096:
        raise InvalidAuthError

    gateway_url = validate_gateway_url(
        str(user_input.get(CONF_GATEWAY_WS_URL, DEFAULT_GATEWAY_WS_URL))
    )
    local_url = validate_local_url(
        str(user_input.get(CONF_HOME_ASSISTANT_URL, DEFAULT_HOME_ASSISTANT_URL))
    )
    session = async_get_clientsession(hass)
    await async_validate_gateway_credentials(
        session,
        gateway_url=gateway_url,
        agent_id=agent_id,
        token=token,
    )

    return {
        CONF_AGENT_ID: agent_id,
        CONF_AGENT_AUTH_TOKEN: token,
        CONF_GATEWAY_WS_URL: gateway_url,
        CONF_HOME_ASSISTANT_URL: local_url,
        CONF_REMOTE_ADMIN_ENABLED: bool(user_input[CONF_REMOTE_ADMIN_ENABLED]),
    }


class REXLiTEConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the REXLiTE config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Create a new REXLiTE connection."""

        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                data = await _validate_input(self.hass, user_input)
            except InvalidAuthError:
                errors["base"] = "invalid_auth"
            except CannotConnectError:
                errors["base"] = "cannot_connect"
            except ProtocolError:
                errors["base"] = "invalid_input"
            else:
                await self.async_set_unique_id(data[CONF_AGENT_ID])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"REXLiTE AI {data[CONF_AGENT_ID]}", data=data
                )
        return self.async_show_form(
            step_id="user", data_schema=_schema(user_input), errors=errors
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> config_entries.ConfigFlowResult:
        """Start reauthentication after the gateway rejects credentials."""

        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Validate and save a replacement enrollment token."""

        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            merged = {**entry.data, **user_input}
            try:
                data = await _validate_input(self.hass, merged)
            except InvalidAuthError:
                errors["base"] = "invalid_auth"
            except CannotConnectError:
                errors["base"] = "cannot_connect"
            except ProtocolError:
                errors["base"] = "invalid_input"
            else:
                return self.async_update_reload_and_abort(entry, data=data)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_AGENT_AUTH_TOKEN): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    )
                }
            ),
            errors=errors,
        )
