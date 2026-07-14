"""Binary sensor platform for REXLiTE."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import REXLiTEConfigEntry
from .entity import REXLiTEEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: REXLiTEConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the REXLiTE connectivity sensor."""

    async_add_entities([REXLiTEConnectedBinarySensor(entry.runtime_data)])


class REXLiTEConnectedBinarySensor(REXLiTEEntity, BinarySensorEntity):
    """Report whether the outbound tunnel is currently connected."""

    _attr_translation_key = "connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: Any) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config.agent_id}_connected"

    @property
    def is_on(self) -> bool:
        """Return the live connection state."""

        return self.coordinator.data.connected
