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


class _WSServerHandshakeError(_ClientError):
    """Stub an aiohttp WebSocket handshake failure."""

    def __init__(self, status: int) -> None:
        super().__init__(f"handshake rejected with status {status}")
        self.status = status


aiohttp.ClientError = _ClientError
aiohttp.WSServerHandshakeError = _WSServerHandshakeError
sys.modules["aiohttp"] = aiohttp

_load_module("const", "const.py")
protocol = _load_module("protocol", "protocol.py")
network = _load_module("network", "network.py")
runtime = _load_module("runtime", "runtime.py")


class RuntimeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """Protect remote access mode changes from stale tunnel sessions."""

    def _client(
        self,
        enabled: bool,
        detector: Any = None,
        auth_failure_callback: Any = None,
        state_callback: Any = None,
    ) -> Any:
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
            state_callback=state_callback or (lambda state: None),
            task_factory=lambda coroutine, name: None,
            auth_failure_callback=auth_failure_callback,
            ipc_ip_detector=detector,
        )

    async def test_unauthorized_handshake_stops_retry_and_requests_reauth_once(
        self,
    ) -> None:
        notifications: list[str] = []
        states: list[Any] = []
        client = self._client(
            False,
            auth_failure_callback=lambda: notifications.append("reauth"),
            state_callback=states.append,
        )

        async def reject() -> None:
            raise _WSServerHandshakeError(401)

        client._run_once = reject

        await client._run_forever()
        await client._run_forever()

        self.assertEqual(notifications, ["reauth"])
        self.assertTrue(client.state.authentication_failed)
        self.assertFalse(client.state.connected)
        self.assertEqual(client.state.reconnect_attempt, 0)
        self.assertEqual(client.state.last_error, "authentication_failed (HTTP 401)")
        self.assertTrue(states[-1].authentication_failed)

    async def test_transient_failure_remains_retryable_and_clears_auth_state(
        self,
    ) -> None:
        client = self._client(False)
        client._set_state(authentication_failed=True)

        attempt = client._record_transient_failure(ConnectionError("offline"), 1)

        self.assertEqual(attempt, 1)
        self.assertFalse(client.state.authentication_failed)
        self.assertEqual(client.state.reconnect_attempt, 2)
        self.assertIn("ConnectionError", client.state.last_error)

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

    async def test_preconfigured_health_session_can_be_activated_later(self) -> None:
        client = self._client(False)
        original_identity = (client._config.agent_id, client._config.auth_token)
        calls: list[str] = []

        async def stop() -> None:
            calls.append("stop")

        async def start() -> None:
            calls.append("start")

        client.async_stop = stop
        client.async_start = start

        await client.async_set_remote_admin(True)

        self.assertEqual(calls, ["stop", "start"])
        self.assertEqual(
            (client._config.agent_id, client._config.auth_token), original_identity
        )
        self.assertTrue(client.state.remote_admin_enabled)

    def test_service_state_distinguishes_staging_from_live_access(self) -> None:
        staged = runtime.RuntimeState(connected=True, remote_admin_enabled=False)
        active = runtime.RuntimeState(connected=True, remote_admin_enabled=True)
        offline = runtime.RuntimeState(connected=False, remote_admin_enabled=True)
        rejected = runtime.RuntimeState(
            connected=False,
            remote_admin_enabled=False,
            authentication_failed=True,
        )

        self.assertEqual(staged.cloud_service_state, "ready_for_activation")
        self.assertEqual(active.cloud_service_state, "connected")
        self.assertEqual(offline.cloud_service_state, "disconnected")
        self.assertEqual(rejected.cloud_service_state, "verification_required")

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

    async def test_network_can_start_unavailable_then_publish_new_dhcp_address(
        self,
    ) -> None:
        detected = iter((None, network.IPCLanAddress("10.20.30.40", "default_route")))
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

        self.assertEqual(
            [message_type for message_type, _ in sent],
            ["hello", "hello", "heartbeat"],
        )
        self.assertEqual(sent[0][1]["metadata"]["ipc_lan_ip_status"], "unavailable")
        self.assertEqual(
            sent[1][1]["metadata"]["ipc_lan_url"], "http://10.20.30.40:8123"
        )

    async def test_reboot_with_new_ip_preserves_cloud_identity(self) -> None:
        before = self._client(
            True,
            lambda _: network.IPCLanAddress("192.168.10.20", "default_route"),
        )
        after = self._client(
            True,
            lambda _: network.IPCLanAddress("10.20.30.40", "default_route"),
        )
        published: list[dict[str, Any]] = []

        async def capture(
            message_type: str,
            request_id: str = "",
            payload: Mapping[str, Any] | None = None,
        ) -> None:
            self.assertEqual(message_type, "hello")
            published.append(dict(payload or {}))

        before._send = capture
        after._send = capture

        await before._send_hello()
        await after._send_hello()

        before_url = protocol.agent_websocket_url(
            before._config.gateway_url,
            agent_id=before._config.agent_id,
            version="0.1.6",
            remote_admin_enabled=before._config.remote_admin_enabled,
        )
        after_url = protocol.agent_websocket_url(
            after._config.gateway_url,
            agent_id=after._config.agent_id,
            version="0.1.6",
            remote_admin_enabled=after._config.remote_admin_enabled,
        )
        self.assertEqual(before_url, after_url)
        self.assertNotIn("192.168.10.20", before_url)
        self.assertNotIn("10.20.30.40", after_url)
        self.assertEqual(
            published[0]["metadata"]["ipc_lan_url"],
            "http://192.168.10.20:8123",
        )
        self.assertEqual(
            published[1]["metadata"]["ipc_lan_url"],
            "http://10.20.30.40:8123",
        )
