"""Discovery identity, exclusion and bounded concurrency checks."""

import asyncio
import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

PATH = Path(__file__).parents[1] / "custom_components/rexlite/device_discovery.py"
spec = importlib.util.spec_from_file_location("device_discovery_test", PATH)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def info(**changes):
    return SimpleNamespace(
        ssdp_location="http://192.168.68.1:1900/description.xml",
        upnp={
            "manufacturer": "TP-Link",
            "modelName": "X55",
            "friendlyName": "TP-Link Router",
            "presentationURL": "http://192.168.68.1",
            **changes,
        },
    )


class CandidateTests(unittest.TestCase):
    def test_deco_uses_actual_presentation_address(self):
        row = m.deco_candidate(info(), set())
        self.assertEqual(row["host"], "http://192.168.68.1")
        self.assertEqual(row["handler"], "tplink_deco")

    def test_existing_deco_and_other_tplink_router_are_not_pending(self):
        self.assertIsNone(m.deco_candidate(info(), {"192.168.68.1"}))
        self.assertIsNone(m.deco_candidate(info(modelName="Archer AX55"), set()))
        self.assertIsNone(m.deco_candidate(info(manufacturer="Unknown"), set()))

    def test_never_suggest_unsafe_credential_destinations(self):
        for value in (
            "http://127.0.0.1",
            "http://169.254.169.254",
            "http://8.8.8.8",
            "http://user:password@192.168.1.1",
            "javascript:alert(1)",
            "http://192.168.1.1:99999",
            "http://0.0.0.0",
            "http://224.0.0.1",
        ):
            self.assertEqual(m.local_origin(value), "")


class EndpointTests(unittest.TestCase):
    def test_only_local_advertised_tcp_services_are_probed(self):
        self.assertEqual(m.discovery_endpoint(info()), ("192.168.68.1", 1900))
        for host in (
            "8.8.8.8",
            "127.0.0.1",
            "169.254.169.254",
            "router.local",
            "0.0.0.0",
        ):
            self.assertIsNone(
                m.discovery_endpoint(
                    SimpleNamespace(type="_http._tcp.local.", host=host, port=80)
                )
            )
        self.assertIsNone(
            m.discovery_endpoint(
                SimpleNamespace(
                    type="_service._udp.local.", host="192.168.1.2", port=9999
                )
            )
        )
        self.assertIsNone(
            m.discovery_endpoint(
                SimpleNamespace(type="_http._tcp.local.", host="192.168.1.2", port=0)
            )
        )


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_closes_connection_and_failure_is_not_online(self):
        writer = SimpleNamespace(close=Mock(), wait_closed=AsyncMock())
        with patch.object(
            asyncio, "open_connection", AsyncMock(return_value=(None, writer))
        ):
            self.assertTrue(await m.endpoint_online("192.168.1.2", 80))
            writer.close.assert_called_once()
            writer.wait_closed.assert_awaited_once()
        with patch.object(asyncio, "open_connection", AsyncMock(side_effect=OSError())):
            self.assertFalse(await m.endpoint_online("192.168.1.2", 80))


class ScanTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_requests_share_scan_and_errors_can_retry(self):
        scanner = m.DeviceDiscovery(None)
        scanner._scan = AsyncMock(return_value={"flows": [], "candidates": []})
        results = await asyncio.gather(scanner.scan(), scanner.scan())
        self.assertEqual(results[0], results[1])
        scanner._scan.assert_awaited_once()
        scanner.finished = 0
        scanner._scan.side_effect = OSError()
        with self.assertRaises(OSError):
            await scanner.scan()
        scanner._scan.side_effect = None
        await scanner.scan()
        self.assertEqual(scanner._scan.await_count, 3)
