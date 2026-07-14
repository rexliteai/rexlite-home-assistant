"""Switch platform for REXLiTE."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import REXLiTEConfigEntry
from .const import CONF_REMOTE_ADMIN_ENABLED
from .entity import REXLiTEEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: REXLiTEConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the REXLiTE remote-administration switch."""

    async_add_entities([REXLiTERemoteAdminSwitch(entry)])


class REXLiTERemoteAdminSwitch(REXLiTEEntity, SwitchEntity):
    """Gate all remote control while keeping health monitoring connected."""

    _attr_translation_key = "remote_admin"
    _attr_icon = "mdi:remote"

    def __init__(self, entry: REXLiTEConfigEntry) -> None:
        super().__init__(entry.runtime_data)
        self._entry = entry
        self._attr_unique_id = f"{entry.runtime_data.config.agent_id}_remote_admin"

    @property
    def is_on(self) -> bool:
        """Return whether remote administration is enabled."""

        return self.coordinator.data.remote_admin_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable authenticated remote control."""

        await self._async_set_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable control but preserve connection and health reporting."""

        await self._async_set_enabled(False)

    async def _async_set_enabled(self, enabled: bool) -> None:
        options = {**self._entry.options, CONF_REMOTE_ADMIN_ENABLED: enabled}
        self.hass.config_entries.async_update_entry(self._entry, options=options)
        await self.coordinator.async_set_remote_admin(enabled)
