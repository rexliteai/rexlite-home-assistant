"""LAN IPv4 discovery regression tests."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).parents[1] / "custom_components" / "rexlite" / "network.py"
SPEC = importlib.util.spec_from_file_location("rexlite_network_test", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Unable to load network.py")
network = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = network
SPEC.loader.exec_module(network)


class IPCLanDetectionTests(unittest.TestCase):
    def test_prefers_the_live_route_over_a_stale_configured_address(self) -> None:
        result = network.detect_ipc_lan_ipv4(
            "http://192.168.68.65:8123",
            route_probe=lambda: "192.168.1.107",
            hostname_probe=lambda: (),
            assigned_probe=lambda _: True,
            containerized=False,
        )

        self.assertEqual(result.address, "192.168.1.107")
        self.assertEqual(result.source, "default_route")

    def test_uses_configured_address_when_live_probes_are_unavailable(
        self,
    ) -> None:
        result = network.detect_ipc_lan_ipv4(
            "http://192.168.68.65:8123",
            route_probe=lambda: None,
            hostname_probe=lambda: (),
            assigned_probe=lambda _: True,
            containerized=False,
        )

        self.assertEqual(result.address, "192.168.68.65")
        self.assertEqual(result.source, "home_assistant_url")

    def test_rejects_a_stale_configured_address_not_assigned_to_the_host(self) -> None:
        result = network.detect_ipc_lan_ipv4(
            "http://192.168.68.65:8123",
            route_probe=lambda: None,
            hostname_probe=lambda: (),
            assigned_probe=lambda _: False,
            containerized=False,
        )

        self.assertIsNone(result)

    def test_replaces_loopback_with_the_default_route_address(self) -> None:
        result = network.detect_ipc_lan_ipv4(
            "http://127.0.0.1:8123",
            route_probe=lambda: "192.168.1.107",
            hostname_probe=lambda: (),
            containerized=False,
        )

        self.assertEqual(result.address, "192.168.1.107")

    def test_rejects_the_docker_bridge_gateway(self) -> None:
        result = network.detect_ipc_lan_ipv4(
            "http://172.17.0.1:8123",
            route_probe=lambda: "172.30.33.4",
            hostname_probe=lambda: ("172.17.0.2", "192.168.68.65"),
            containerized=True,
        )

        self.assertEqual(result.address, "192.168.68.65")
        self.assertEqual(result.source, "hostname")

    def test_rejects_public_and_ipv6_candidates(self) -> None:
        result = network.detect_ipc_lan_ipv4(
            "https://8.8.8.8:8123",
            route_probe=lambda: "2001:db8::1",
            hostname_probe=lambda: ("169.254.1.2", "0.0.0.0"),
            containerized=False,
        )

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
