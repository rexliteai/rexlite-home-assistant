"""REXLiTE integration for Home Assistant."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from typing import Any

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

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# HA's backup manager listens here for MAX'Is 雲端 coming or going (backup.py).
DATA_BACKUP_AGENT_LISTENERS = f"{DOMAIN}.backup_agent_listeners"


async def _async_optional(name: str, start: Callable[[], Any]) -> Any:
    """Start one feature; a failure is logged and never stops the cloud tunnel."""

    try:
        result = start()
        return await result if inspect.isawaitable(result) else result
    except Exception:  # noqa: BLE001 - the tunnel is how a broken hub gets repaired
        _LOGGER.exception("Could not start %s; the cloud service still loads", name)
        return None


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register local administrative commands once per HA instance.

    Each feature is imported and registered on its own: whatever fails here,
    this returns True, or Home Assistant would leave the config entry (the
    cloud tunnel) unloaded with nothing left to repair the hub through.
    """

    def device_discovery() -> None:
        from .device_discovery import register_device_discovery

        register_device_discovery(hass)

    def thread_border_routers() -> None:
        from .thread_border_routers import register_thread_border_routers

        register_thread_border_routers(hass)

    def homekit_accessories() -> None:
        from .homekit_accessories import register_homekit_accessories

        register_homekit_accessories(hass)

    def tapo_ir() -> None:
        from .tapo_ir import register_tapo_ir

        register_tapo_ir(hass)

    def node_red_bridge() -> None:
        from .node_red_bridge import register_node_red_bridge

        register_node_red_bridge(hass)

    async def cameras() -> None:
        from .frigate_bridge import (
            async_register_plate_webhook,
            register_frigate_bridge,
        )

        frigate = register_frigate_bridge(hass)
        # Plates are optional as well; the camera commands stay registered.
        await _async_optional(
            "the license plate webhook",
            lambda: async_register_plate_webhook(hass, frigate),
        )

    def hacs_bind() -> None:
        from .hacs_bind import register_hacs_bind

        register_hacs_bind(hass)

    def knx_deployment() -> None:
        from homeassistant.helpers.start import async_at_started

        from .knx_project_deployment import register_websocket_commands

        deployer = register_websocket_commands(hass)

        async def recover_interrupted_deployment(_hass: HomeAssistant) -> None:
            await deployer.recover()

        async_at_started(hass, recover_interrupted_deployment)

    async def network_traffic() -> None:
        from .network_traffic_api import async_register_network_traffic

        await async_register_network_traffic(hass)

    async def power_events() -> None:
        from .power_events import async_register_power_events

        await async_register_power_events(hass)

    for name, start in (
        ("device discovery", device_discovery),
        ("the Thread border router list", thread_border_routers),
        ("the HomeKit accessory list", homekit_accessories),
        ("the Tapo IR remotes", tapo_ir),
        ("the Node-RED bridge", node_red_bridge),
        ("camera management", cameras),
        ("the HACS binding", hacs_bind),
        ("KNX deployment", knx_deployment),
        ("network traffic", network_traffic),
        ("the power outage record", power_events),
    ):
        await _async_optional(name, start)
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
        entry_id=entry.entry_id,
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

    def notify_backup_agents() -> None:
        # MAX'Is 雲端 is offered while this entry is loaded.
        for listener in list(hass.data.get(DATA_BACKUP_AGENT_LISTENERS, [])):
            listener()

    entry.async_on_unload(entry.async_on_state_change(notify_backup_agents))
    await coordinator.async_start()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: REXLiTEConfigEntry) -> bool:
    """Unload a REXLiTE config entry without leaking background tasks."""

    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
