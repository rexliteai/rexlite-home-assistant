"""Diagnostics support for REXLiTE."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import REXLiTEConfigEntry
from .const import CONF_AGENT_AUTH_TOKEN


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: REXLiTEConfigEntry
) -> dict[str, Any]:
    """Return useful diagnostics without exposing enrollment credentials."""

    data = {
        key: "**REDACTED**" if key == CONF_AGENT_AUTH_TOKEN else value
        for key, value in entry.data.items()
    }
    state = entry.runtime_data.data
    return {
        "entry": {"data": data, "options": dict(entry.options)},
        "runtime": {
            "connected": state.connected,
            "remote_admin_enabled": state.remote_admin_enabled,
            "access_mode": state.access_mode,
            "reconnect_attempt": state.reconnect_attempt,
            "last_connected_at": state.last_connected_at,
            "last_error": state.last_error,
        },
    }
