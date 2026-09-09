"""Exercise cloud session recovery with real aiohttp sockets and synthetic data.

Run separately from the dependency-stubbed unit suite in an official HA runtime.
No production host, credential, or device is contacted.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import types
import unittest
from dataclasses import replace
from pathlib import Path

import aiohttp
from aiohttp import web

package = types.ModuleType("rexlite_cloud_socket_test")
package.__path__ = [str(Path(__file__).parents[1] / "custom_components/rexlite")]
sys.modules[package.__name__] = package
runtime = importlib.import_module(f"{package.__name__}.runtime")
protocol = importlib.import_module(f"{package.__name__}.protocol")


class CloudSocketTests(unittest.IsolatedAsyncioTestCase):
    """Verify recovery, session isolation, and bounded shutdown over real sockets."""

    async def asyncSetUp(self) -> None:
        self.sockets = []
        self.received = []
        self.tasks = []
        self.states = []
        self.changed = asyncio.Event()
        self.runner = web.AppRunner(self._app())
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.session = aiohttp.ClientSession()
        self.client = runtime.REXLiTETunnelClient(
            self.session,
            runtime.TunnelConfig(
                agent_id="synthetic-cloud-test",
                auth_token="synthetic-token",
                gateway_url=f"ws://127.0.0.1:{port}/ws/agent",
                home_assistant_url="http://127.0.0.1:8123",
                home_assistant_version="2026.1.0",
                heartbeat_interval=0.02,
                reconnect_delay=0.01,
                reconnect_max_delay=0.03,
                request_timeout=0.2,
            ),
            self._state_changed,
            self._create_task,
            ipc_ip_detector=lambda _url: None,
        )

    def _app(self):
        app = web.Application()
        app.router.add_get("/ws/agent", self._gateway)
        return app

    async def _gateway(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        index = len(self.sockets)
        self.sockets.append(ws)
        self.changed.set()
        async for message in ws:
            if message.type == aiohttp.WSMsgType.TEXT:
                envelope = json.loads(message.data)
                self.received.append((index, envelope))
                self.changed.set()
                if envelope["type"] == "heartbeat":
                    await ws.send_str(
                        protocol.make_envelope(
                            "heartbeat_ack", payload={"status": "ok"}
                        )
                    )
        return ws

    def _create_task(self, coroutine, name):
        task = asyncio.create_task(coroutine, name=name)
        self.tasks.append(task)
        return task

    def _state_changed(self, state):
        self.states.append(state)
        self.changed.set()

    async def _wait(self, predicate):
        async with asyncio.timeout(5):
            while not predicate():
                self.changed.clear()
                await self.changed.wait()

    async def _start(self):
        await self.client.async_start()
        await self._wait(lambda: self.client.state.connected and self.received)

    async def asyncTearDown(self) -> None:
        try:
            await asyncio.wait_for(self.client.async_stop(), 2)
        finally:
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            for ws in self.sockets:
                await ws.close()
            await self.session.close()
            await self.runner.cleanup()

    async def test_twenty_disconnects_recover_without_leaking_session_tasks(self):
        await self._start()
        for _ in range(20):
            previous = len(self.sockets)
            await self.sockets[-1].close()
            await self._wait(
                lambda previous=previous: (
                    len(self.sockets) > previous and self.client.state.connected
                )
            )
        await self.client.async_stop()
        self.assertTrue(all(task.done() for task in self.tasks))
        self.assertFalse(self.client._handler_tasks)
        self.assertFalse(self.client._streams)
        self.assertIsNone(self.client._ws)

    async def test_disconnect_cancels_old_request_before_new_session(self):
        await self._start()
        release = asyncio.Event()
        cancelled = asyncio.Event()

        async def old_request():
            try:
                await release.wait()
                await self.client._send("proxy_response", "old-request", {})
            except asyncio.CancelledError:
                cancelled.set()
                raise

        self.client._spawn_handler(old_request())
        await asyncio.sleep(0)
        await self.sockets[-1].close()
        await self._wait(lambda: len(self.sockets) == 2 and self.client.state.connected)
        release.set()
        await asyncio.sleep(0.05)
        self.assertTrue(cancelled.is_set())
        self.assertFalse(
            any(msg.get("request_id") == "old-request" for _, msg in self.received)
        )

    async def test_heartbeat_failure_reconnects_and_resumes_heartbeats(self):
        original = self.client._send_heartbeat
        failures = 0

        async def fail_once():
            nonlocal failures
            if failures == 0:
                failures += 1
                raise ConnectionError("synthetic send failure")
            await original()

        self.client._send_heartbeat = fail_once
        await self._start()
        await self._wait(
            lambda: any(i == 1 and m["type"] == "heartbeat" for i, m in self.received)
        )
        self.assertTrue(self.client.state.connected)
        self.assertFalse(self.client._heartbeat_task.done())

    async def test_stop_cancels_pending_request_promptly(self):
        await self._start()
        self.client._spawn_handler(asyncio.Event().wait())
        await asyncio.sleep(0)
        await asyncio.wait_for(self.client.async_stop(), 1)
        self.assertTrue(all(task.done() for task in self.tasks))
        self.assertFalse(self.client.state.connected)

    async def test_initial_hello_failure_cleans_up_before_retry(self):
        original = self.client._send_hello
        failures = 0

        async def fail_once(metadata=None):
            nonlocal failures
            if failures == 0:
                failures += 1
                raise ConnectionError("synthetic hello failure")
            await original(metadata)

        self.client._send_hello = fail_once
        await self._start()
        self.assertGreaterEqual(len(self.sockets), 2)
        self.assertTrue(any(not state.connected for state in self.states))
        await self.client.async_stop()
        self.assertIsNone(self.client._connected_since)
        self.assertTrue(all(task.done() for task in self.tasks))

    async def test_send_backpressure_has_a_deadline(self):
        await self._start()
        self.client._config = replace(self.client._config, request_timeout=0.03)
        async with self.client._send_lock:
            with self.assertRaises(TimeoutError):
                await self.client._send("heartbeat")

    async def test_healthy_session_keeps_heartbeats_on_single_connection(self):
        await self._start()
        await self._wait(
            lambda: sum(m["type"] == "heartbeat" for _, m in self.received) >= 25
        )
        self.assertEqual(len(self.sockets), 1)
        self.assertTrue(self.client.state.connected)
        self.assertEqual(self.client.state.reconnect_attempt, 0)

    async def test_access_mode_changes_cancel_pending_work_and_keep_identity(self):
        await self._start()
        identity = (self.client._config.agent_id, self.client._config.auth_token)
        for enabled in (True, False, True):
            previous = len(self.sockets)
            self.client._spawn_handler(asyncio.Event().wait())
            await asyncio.wait_for(self.client.async_set_remote_admin(enabled), 1)
            await self._wait(
                lambda previous=previous: (
                    len(self.sockets) > previous and self.client.state.connected
                )
            )
            self.assertFalse(self.client._handler_tasks)
            self.assertEqual(self.client.state.remote_admin_enabled, enabled)
            self.assertEqual(
                (self.client._config.agent_id, self.client._config.auth_token), identity
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
