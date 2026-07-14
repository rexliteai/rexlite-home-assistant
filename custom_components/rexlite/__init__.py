"""REXLiTE integration for Home Assistant."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import __version__ as home_assistant_version
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_AGENT_AUTH_TOKEN,
    CONF_AGENT_ID,
    CONF_GATEWAY_WS_URL,
    CONF_HOME_ASSISTANT_URL,
    CONF_REMOTE_ADMIN_ENABLED,
    DEFAULT_GATEWAY_WS_URL,
    DEFAULT_HOME_ASSISTANT_URL,
    DEFAULT_REMOTE_ADMIN_ENABLED,
    PLATFORMS,
)
from .coordinator import REXLiTECoordinator
from .runtime import TunnelConfig

type REXLiTEConfigEntry = ConfigEntry[REXLiTECoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: REXLiTEConfigEntry) -> bool:
    """Set up REXLiTE from a config entry."""

    remote_admin_enabled = bool(
        entry.options.get(
            CONF_REMOTE_ADMIN_ENABLED,
            entry.data.get(CONF_REMOTE_ADMIN_ENABLED, DEFAULT_REMOTE_ADMIN_ENABLED),
        )
    )
    config = TunnelConfig(
        agent_id=str(entry.data[CONF_AGENT_ID]),
        auth_token=str(entry.data[CONF_AGENT_AUTH_TOKEN]),
        gateway_url=str(entry.data.get(CONF_GATEWAY_WS_URL, DEFAULT_GATEWAY_WS_URL)),
        home_assistant_url=str(
            entry.data.get(CONF_HOME_ASSISTANT_URL, DEFAULT_HOME_ASSISTANT_URL)
        ),
        home_assistant_version=home_assistant_version,
        remote_admin_enabled=remote_admin_enabled,
    )
    coordinator = REXLiTECoordinator(hass, entry, async_get_clientsession(hass), config)
    entry.runtime_data = coordinator
    await coordinator.async_start()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: REXLiTEConfigEntry) -> bool:
    """Unload a REXLiTE config entry without leaking background tasks."""

    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
