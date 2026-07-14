"""State coordinator for the REXLiTE integration."""

from __future__ import annotations

import logging
from dataclasses import replace

from aiohttp import ClientSession
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .runtime import REXLiTETunnelClient, RuntimeState, TunnelConfig

_LOGGER = logging.getLogger(__name__)


class REXLiTECoordinator(DataUpdateCoordinator[RuntimeState]):
    """Bridge push-based tunnel state into Home Assistant entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        session: ClientSession,
        config: TunnelConfig,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name="REXLiTE",
            update_interval=None,
        )
        self.config = config
        self.client = REXLiTETunnelClient(
            session,
            config,
            self._handle_state,
            lambda coroutine, name: entry.async_create_background_task(
                hass, coroutine, name=name
            ),
        )
        self.async_set_updated_data(self.client.state)

    async def async_start(self) -> None:
        """Start the long-running tunnel client."""

        await self.client.async_start()

    async def async_shutdown(self) -> None:
        """Stop all runtime tasks."""

        await self.client.async_stop()
        await super().async_shutdown()

    async def async_set_remote_admin(self, enabled: bool) -> None:
        """Update the control gate while preserving the health connection."""

        self.config = replace(self.config, remote_admin_enabled=enabled)
        await self.client.async_set_remote_admin(enabled)

    def _handle_state(self, state: RuntimeState) -> None:
        self.async_set_updated_data(state)
