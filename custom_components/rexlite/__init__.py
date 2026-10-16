"""REXLiTE integration for Home Assistant."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import __version__ as home_assistant_version
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

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
    PLATFORMS,
)
from .coordinator import REXLiTECoordinator
from .runtime import TunnelConfig

type REXLiTEConfigEntry = ConfigEntry[REXLiTECoordinator]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register local administrative deployment commands once per HA instance."""
    from homeassistant.helpers.start import async_at_started

    from .device_discovery import register_device_discovery
    from .frigate_bridge import register_frigate_bridge
    from .hacs_bind import register_hacs_bind
    from .hacs_yaml import register_hacs_yaml
    from .knx_project_deployment import register_websocket_commands
    from .network_traffic_api import async_register_network_traffic
    from .node_red_bridge import register_node_red_bridge

    register_device_discovery(hass)
    register_node_red_bridge(hass)
    register_frigate_bridge(hass)
    register_hacs_bind(hass)
    deployer = register_websocket_commands(hass)
    # Both write configuration.yaml, so they share the deployment lock.
    yaml_packages = register_hacs_yaml(hass, deployer.lock)
    try:
        await yaml_packages.snapshot()
    except Exception:  # noqa: BLE001 - setup must not fail over it
        _LOGGER.exception("Could not read the HACS YAML packages")
    await async_register_network_traffic(hass)

    async def recover_interrupted_deployment(_hass: HomeAssistant) -> None:
        try:
            await yaml_packages.recover()
        except Exception:  # noqa: BLE001 - the KNX recovery still runs
            _LOGGER.exception("HACS YAML rollback failed")
        await deployer.recover()

    async_at_started(hass, recover_interrupted_deployment)
    return True


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
    session = async_get_clientsession(hass)
    try:
        await async_validate_gateway_credentials(
            session,
            gateway_url=config.gateway_url,
            agent_id=config.agent_id,
            token=config.auth_token,
        )
    except InvalidAuthError as err:
        raise ConfigEntryAuthFailed(
            "REXLiTE AI rejected the stored service credential"
        ) from err
    except CannotConnectError as err:
        raise ConfigEntryNotReady(
            "REXLiTE AI Cloud Service is temporarily unavailable"
        ) from err

    coordinator = REXLiTECoordinator(hass, entry, session, config)
    entry.runtime_data = coordinator
    entry.async_on_unload(coordinator.async_shutdown)
    await coordinator.async_start()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: REXLiTEConfigEntry) -> bool:
    """Unload a REXLiTE config entry without leaking background tasks."""

    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
