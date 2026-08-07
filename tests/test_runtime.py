"""Tunnel lifecycle regression tests without Home Assistant dependencies."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any

PACKAGE_PATH = Path(__file__).parents[1] / "custom_components" / "rexlite"
PACKAGE_NAME = "rexlite_runtime_test"


def _load_module(name: str, filename: str) -> types.ModuleType:
    """Load one integration module under an isolated test package."""

    module_name = f"{PACKAGE_NAME}.{name}"
    spec = importlib.util.spec_from_file_location(module_name, PACKAGE_PATH / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


package = types.ModuleType(PACKAGE_NAME)
package.__path__ = [str(PACKAGE_PATH)]
sys.modules[PACKAGE_NAME] = package

homeassistant = types.ModuleType("homeassistant")
homeassistant_const = types.ModuleType("homeassistant.const")


class _Platform:
    SENSOR = "sensor"
    BINARY_SENSOR = "binary_sensor"
    SWITCH = "switch"


homeassistant_const.Platform = _Platform
homeassistant.const = homeassistant_const
sys.modules["homeassistant"] = homeassistant
sys.modules["homeassistant.const"] = homeassistant_const

aiohttp = types.ModuleType("aiohttp")


class _ClientError(Exception):
    """Stub aiohttp client error."""


aiohttp.ClientError = _ClientError
sys.modules["aiohttp"] = aiohttp

_load_module("const", "const.py")
_load_module("protocol", "protocol.py")
network = _load_module("network", "network.py")
runtime = _load_module("runtime", "runtime.py")


class RuntimeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """Protect remote access mode changes from stale tunnel sessions."""

    def _client(self, enabled: bool, detector: Any = None) -> Any:
        return runtime.REXLiTETunnelClient(
            session=object(),
            config=runtime.TunnelConfig(
                agent_id="hub-1",
                auth_token="token",
                gateway_url="wss://gateway.example/ws/agent",
                home_assistant_url="http://127.0.0.1:8123",
                home_assistant_version="2026.7.2",
                remote_admin_enabled=enabled,
            ),
            state_callback=lambda state: None,
            task_factory=lambda coroutine, name: None,
            ipc_ip_detector=detector,
        )

    async def test_access_mode_change_restarts_the_authenticated_session(self) -> None:
        client = self._client(True)
        calls: list[str] = []

        async def stop() -> None:
            calls.append("stop")

        async def start() -> None:
            calls.append("start")

        client.async_stop = stop
        client.async_start = start

        await client.async_set_remote_admin(False)

        self.assertEqual(calls, ["stop", "start"])
        self.assertFalse(client._config.remote_admin_enabled)
        self.assertFalse(client.state.remote_admin_enabled)

    async def test_unchanged_access_mode_does_not_restart_the_session(self) -> None:
        client = self._client(False)
        calls: list[str] = []

        async def stop() -> None:
            calls.append("stop")

        async def start() -> None:
            calls.append("start")

        client.async_stop = stop
        client.async_start = start

        await client.async_set_remote_admin(False)

        self.assertEqual(calls, [])

    async def test_ip_change_republishes_hello_before_heartbeat(self) -> None:
        detected = iter(
            (
                network.IPCLanAddress("192.168.68.65", "default_route"),
                network.IPCLanAddress("192.168.68.65", "default_route"),
                network.IPCLanAddress("192.168.1.107", "default_route"),
            )
        )
        client = self._client(False, lambda _: next(detected))
        sent: list[tuple[str, dict[str, Any]]] = []

        async def send(
            message_type: str,
            request_id: str = "",
            payload: Mapping[str, Any] | None = None,
        ) -> None:
            sent.append((message_type, dict(payload or {})))

        client._send = send

        await client._send_hello()
        await client._send_heartbeat()
        await client._send_heartbeat()

        self.assertEqual(
            [message_type for message_type, _ in sent],
            ["hello", "heartbeat", "hello", "heartbeat"],
        )
        self.assertEqual(
            sent[0][1]["metadata"]["ipc_lan_url"],
            "http://192.168.68.65:8123",
        )
        self.assertEqual(
            sent[2][1]["metadata"]["ipc_lan_url"],
            "http://192.168.1.107:8123",
        )
