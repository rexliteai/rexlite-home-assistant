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
            flow=SimpleNamespace(
                async_progress=lambda: [{"flow_id": "cached-device"}],
                async_progress_by_init_data_type=lambda *args: [],
            ),
        )
        callback = None
        remove = Mock()
        browser = SimpleNamespace(async_cancel=AsyncMock())
        device = SimpleNamespace(
            ssdp_location="http://192.168.68.1/description.xml",
            upnp={"manufacturer": "TP-Link", "modelName": "X55"},
        )

        async def register(hass, handler, match_dict):
            nonlocal callback
            assert match_dict["ST"] in m.ROUTER_SEARCH_TYPES
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
            assert results[0]["flows"] == []
            assert results[0]["candidates"][0]["host"] == "http://192.168.68.1"
            assert results[0]["candidates"][0]["installed"] is True
            scanner.async_scan.assert_awaited_once()
            assert remove.call_count == len(m.ROUTER_SEARCH_TYPES)

        # A slow cached gateway must not suppress native probes or mDNS.
        async def slow_register(hass, handler, match_dict):
            await asyncio.sleep(60)

        from homeassistant.components import zeroconf

        scanner.async_scan.reset_mock(side_effect=True)
        scanner.async_scan.side_effect = None
        with (
            patch.object(m, "CALLBACK_SETUP_TIMEOUT", 0.01),
            patch.object(ssdp, "async_register_callback", slow_register),
            patch(
                "homeassistant.loader.async_get_zeroconf",
                AsyncMock(return_value={"_http._tcp.local.": []}),
            ),
            patch.object(
                zeroconf,
                "async_get_async_instance",
                AsyncMock(return_value=SimpleNamespace(zeroconf=object())),
            ),
            patch(
                "zeroconf.asyncio.AsyncServiceBrowser", return_value=browser
            ) as browse,
        ):
            result = await m.DeviceDiscovery(hass).scan()
            assert result["flows"] == []
            assert result["warnings"] == ["部分路由器辨識逾時"]
            scanner.async_scan.assert_awaited_once()
            browse.assert_called_once()
            browser.async_cancel.assert_awaited_once()
        # Only a fresh connection to the service endpoint verifies a pending flow.
        from ipaddress import ip_address

        from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

        online = ZeroconfServiceInfo(
            ip_address=ip_address("192.168.1.10"),
            ip_addresses=[ip_address("192.168.1.10")],
            port=8009,
            hostname="tv.local.",
            type="_googlecast._tcp.local.",
            name="TV",
            properties={},
        )
        offline = ZeroconfServiceInfo(
            ip_address=ip_address("192.168.1.11"),
            ip_addresses=[ip_address("192.168.1.11")],
            port=8009,
            hostname="old.local.",
            type="_googlecast._tcp.local.",
            name="Old TV",
            properties={},
        )
        records = [
            (online, {"flow_id": "online", "context": {"source": "zeroconf"}}),
            (offline, {"flow_id": "offline", "context": {"source": "zeroconf"}}),
        ]

        def match(kind, predicate):
            return [
                flow
                for info, flow in records
                if isinstance(info, kind) and predicate(info)
            ]

        hass.config_entries.flow = SimpleNamespace(
            async_progress=lambda: [flow for _, flow in records],
            async_progress_by_init_data_type=match,
        )
        with patch.object(
            m,
            "endpoint_online",
            AsyncMock(side_effect=lambda host, port: host == "192.168.1.10"),
        ):
            live = await m.online_discovery_flows(hass)
            assert [flow["flow_id"] for flow in live] == ["online"]
            assert live[0]["context"]["rexlite_online"] is True
            assert "rexlite_seen_at" in live[0]["context"]
            assert "rexlite_online" not in records[0][1]["context"]
        assert m.discovery_endpoint(online) == ("192.168.1.10", 8009)
        # Register the real HA schema with its admin-only decorator.
        m.register_device_discovery(hass)
        assert "rexlite/discovery/scan" in hass.data["websocket_api"]
        await hass.async_stop()
    print(
        "HA discovery runtime: fresh replies, host identity, coalescing and cleanup OK"
    )


asyncio.run(main())
