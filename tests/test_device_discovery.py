"""Discovery identity, exclusion and bounded concurrency checks."""

import asyncio
import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
