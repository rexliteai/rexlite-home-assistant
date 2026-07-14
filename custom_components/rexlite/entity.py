"""Shared REXLiTE entity base."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, INTEGRATION_VERSION
from .coordinator import REXLiTECoordinator


class REXLiTEEntity(CoordinatorEntity[REXLiTECoordinator]):
    """Base entity tied to one enrolled REXLiTE hub."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: REXLiTECoordinator) -> None:
        super().__init__(coordinator)
        agent_id = coordinator.config.agent_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, agent_id)},
            name=f"REXLiTE {agent_id}",
            manufacturer="REXLiTE",
            model="Home Assistant Tunnel",
            sw_version=INTEGRATION_VERSION,
            configuration_url="https://github.com/rexliteai/rexlite-home-assistant",
        )
