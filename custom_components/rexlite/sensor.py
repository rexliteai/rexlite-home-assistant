"""Sensor platform for REXLiTE."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import REXLiTEConfigEntry
from .const import (
    ATTR_ACCESS_MODE,
    ATTR_GATEWAY_URL,
    ATTR_LAST_CONNECTED_AT,
    ATTR_LAST_ERROR,
    ATTR_RECONNECT_ATTEMPT,
)
from .entity import REXLiTEEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: REXLiTEConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the REXLiTE status sensor."""

    async_add_entities([REXLiTEConnectionStatusSensor(entry.runtime_data)])


class REXLiTEConnectionStatusSensor(REXLiTEEntity, SensorEntity):
    """Human-readable tunnel state with troubleshooting attributes."""

    _attr_translation_key = "connection_status"
    _attr_icon = "mdi:cloud-lock"

    def __init__(self, coordinator: Any) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config.agent_id}_connection_status"

    @property
    def native_value(self) -> str:
        """Return a stable machine-readable state."""

        return "connected" if self.coordinator.data.connected else "disconnected"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose bounded, non-secret diagnostic state."""

        state = self.coordinator.data
        return {
            ATTR_ACCESS_MODE: state.access_mode,
            ATTR_GATEWAY_URL: self.coordinator.config.gateway_url,
            ATTR_LAST_CONNECTED_AT: state.last_connected_at,
            ATTR_LAST_ERROR: state.last_error,
            ATTR_RECONNECT_ATTEMPT: state.reconnect_attempt,
        }
