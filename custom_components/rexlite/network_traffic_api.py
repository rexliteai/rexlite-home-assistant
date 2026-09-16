"""Admin-managed HA traffic channels, persisted locally and read via WebSocket."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from .network_traffic import MAX_CHANNELS, TrafficSampler, validate_channel

DATA_KEY = "rexlite_network_traffic"


class NetworkTraffic:
    def __init__(self, hass: Any, store: Any) -> None:
        self.hass, self.store = hass, store
        self.channels: dict[str, dict] = {}
        self.sampler = TrafficSampler()
        self.lock = asyncio.Lock()
        self.storage_error = False

    async def load(self) -> None:
        saved = await self.store.async_load()
        if not saved:
            return
        if not isinstance(saved, dict) or not isinstance(saved.get("channels"), list):
            raise ValueError("invalid_network_traffic_storage")
        validated = [validate_channel(c) for c in saved["channels"]]
        if len(validated) > MAX_CHANNELS or len({c["id"] for c in validated}) != len(
            validated
        ):
            raise ValueError("invalid_network_traffic_storage")
        self.channels = {c["id"]: c for c in validated}

    async def configure(self, data: dict) -> None:
        if self.storage_error:
            raise ValueError("network_traffic_storage_unavailable")
        channel = validate_channel(data)
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er

        if channel.get("device_id") and not dr.async_get(self.hass).async_get(
            channel["device_id"]
        ):
            raise ValueError("device_not_found")
        registry = er.async_get(self.hass)
        for field in (
            "rx_entity",
            "tx_entity",
            "discontinuity_entity",
            "uptime_entity",
        ):
            entity = channel.get(field)
            if (
                entity
                and not registry.async_get(entity)
                and self.hass.states.get(entity) is None
            ):
                raise ValueError("entity_not_found")
        async with self.lock:
            updated = {**self.channels, channel["id"]: channel}
            if len(updated) > MAX_CHANNELS:
                raise ValueError("too_many_channels")
            if channel["scope"] == "device" and any(
                c["id"] != channel["id"] and c.get("device_id") == channel["device_id"]
                for c in updated.values()
            ):
                raise ValueError("device_already_configured")
            await self.store.async_save({"channels": list(updated.values())})
            self.channels = updated
            self.sampler.forget(channel["id"])

    async def remove(self, channel_id: str) -> None:
        if self.storage_error:
            raise ValueError("network_traffic_storage_unavailable")
        async with self.lock:
            updated = {key: c for key, c in self.channels.items() if key != channel_id}
            await self.store.async_save({"channels": list(updated.values())})
            self.channels = updated
            self.sampler.forget(channel_id)

    def snapshot(self) -> dict:
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er

        now = datetime.now(UTC).timestamp()
        registry = er.async_get(self.hass)
        states = {}
        for channel in self.channels.values():
            for field in (
                "rx_entity",
                "tx_entity",
                "discontinuity_entity",
                "uptime_entity",
            ):
                entity_id = channel.get(field)
                if not entity_id or entity_id in states:
                    continue
                state = self.hass.states.get(entity_id)
                entity = registry.async_get(entity_id)
                if not state or (entity and entity.disabled_by):
                    continue
                if entity and entity.config_entry_id:
                    entry = self.hass.config_entries.async_get_entry(
                        entity.config_entry_id
                    )
                    if not entry or entry.disabled_by or entry.state.value != "loaded":
                        continue
                if entity and entity.device_id:
                    parent = dr.async_get(self.hass).async_get(entity.device_id)
                    if not parent or parent.disabled_by:
                        continue
                stamp = getattr(state, "last_reported", None) or state.last_updated
                states[entity_id] = {
                    "state": state.state,
                    "timestamp": stamp.timestamp(),
                    "attributes": state.attributes,
                }
        results = []
        for channel in self.channels.values():
            device_id = channel.get("device_id")
            device = dr.async_get(self.hass).async_get(device_id) if device_id else None
            usable = not device_id or (device is not None and not device.disabled_by)
            row = self.sampler.sample(channel, states if usable else {}, now)
            stamp = row.pop("sampleTime")
            row["updatedAt"] = (
                datetime.fromtimestamp(stamp, UTC).isoformat()
                if stamp is not None
                else ""
            )
            results.append(row)
        return {"schemaVersion": 1, "channels": results}


async def async_register_network_traffic(hass: Any) -> NetworkTraffic:
    import voluptuous as vol
    from homeassistant.components import websocket_api
    from homeassistant.helpers.service import async_register_admin_service
    from homeassistant.helpers.storage import Store

    if DATA_KEY in hass.data:
        return hass.data[DATA_KEY]
    manager = NetworkTraffic(hass, Store(hass, 1, DATA_KEY))
    try:
        await manager.load()
    except (ValueError, OSError):
        # Optional telemetry must not prevent the existing cloud tunnel starting.
        # Never replace a corrupt settings file with an empty configuration.
        manager.storage_error = True
    hass.data[DATA_KEY] = manager

    @websocket_api.websocket_command({vol.Required("type"): "rexlite/network_traffic"})
    @websocket_api.require_admin
    @websocket_api.async_response
    async def read(hass, connection, msg):
        if manager.storage_error:
            connection.send_error(
                msg["id"],
                "network_traffic_unavailable",
                "Network traffic settings could not be loaded",
            )
            return
        connection.send_result(msg["id"], manager.snapshot())

    async def configure(call):
        from homeassistant.exceptions import HomeAssistantError

        try:
            await manager.configure(dict(call.data))
        except (ValueError, OSError) as err:
            raise HomeAssistantError("network_traffic_configuration_failed") from err

    async def remove(call):
        from homeassistant.exceptions import HomeAssistantError

        try:
            await manager.remove(call.data["id"])
        except (ValueError, OSError) as err:
            raise HomeAssistantError("network_traffic_configuration_failed") from err

    async_register_admin_service(
        hass,
        "rexlite",
        "configure_network_traffic",
        configure,
        schema=vol.Schema(validate_channel),
    )
    async_register_admin_service(
        hass,
        "rexlite",
        "remove_network_traffic",
        remove,
        schema=vol.Schema({vol.Required("id"): str}),
    )
    websocket_api.async_register_command(hass, read)
    return manager
