"""Exercise real HA discovery interfaces with bounded synthetic LAN responses."""

import asyncio
import importlib.util
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components import ssdp
from homeassistant.components.ssdp.const import SSDP_SCANNER
from homeassistant.core import HomeAssistant

PATH = Path(__file__).parents[1] / "custom_components/rexlite/device_discovery.py"
spec = importlib.util.spec_from_file_location("discovery_runtime", PATH)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


async def main():
    with tempfile.TemporaryDirectory() as directory:
        hass = HomeAssistant(directory)
        hass.config_entries = SimpleNamespace(
            async_entries=lambda domain: [],
            flow=SimpleNamespace(async_progress=lambda: [{"flow_id": "new-device"}]),
        )
        callback = None
        remove = Mock()
        browser = SimpleNamespace(async_cancel=AsyncMock())
        device = SimpleNamespace(
            ssdp_location="http://192.168.68.1/description.xml",
            upnp={"manufacturer": "TP-Link", "modelName": "X55"},
        )

        async def register(hass, handler):
            nonlocal callback
            callback = handler
            await handler(device, ssdp.SsdpChange.ALIVE)
            return remove

        async def search():
            await callback(device, ssdp.SsdpChange.UPDATE)

        scanner = SimpleNamespace(async_scan=AsyncMock(side_effect=search))
        hass.data["ssdp"] = {SSDP_SCANNER: scanner}
        with (
            patch.object(ssdp, "async_register_callback", register),
            patch(
                "homeassistant.loader.async_get_zeroconf", AsyncMock(return_value={})
            ),
            patch("homeassistant.loader.async_get_integration", AsyncMock()),
            patch("zeroconf.asyncio.AsyncServiceBrowser", return_value=browser),
        ):
            manager = m.DeviceDiscovery(hass)
            results = await asyncio.gather(manager.scan(), manager.scan())
            assert results[0] == results[1]
            assert results[0]["flows"] == [{"flow_id": "new-device"}]
            assert results[0]["candidates"][0]["host"] == "http://192.168.68.1"
            assert results[0]["candidates"][0]["installed"] is True
            scanner.async_scan.assert_awaited_once()
            remove.assert_called_once()
        # Register the real HA schema with its admin-only decorator.
        m.register_device_discovery(hass)
        assert "rexlite/discovery/scan" in hass.data["websocket_api"]
        await hass.async_stop()
    print(
        "HA discovery runtime: fresh replies, host identity, coalescing and cleanup OK"
    )


asyncio.run(main())
