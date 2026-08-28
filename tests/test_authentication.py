"""Credential validation regression tests without Home Assistant dependencies."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from typing import Any

PACKAGE_PATH = Path(__file__).parents[1] / "custom_components" / "rexlite"
PACKAGE_NAME = "rexlite_authentication_test"


def _load_module(name: str, filename: str) -> types.ModuleType:
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

aiohttp = types.ModuleType("aiohttp")


class _ClientError(Exception):
    """Stub aiohttp transport error."""


class _ClientTimeout:
    def __init__(self, *, total: int) -> None:
        self.total = total


aiohttp.ClientError = _ClientError
aiohttp.ClientSession = object
aiohttp.ClientTimeout = _ClientTimeout
sys.modules["aiohttp"] = aiohttp

_load_module("protocol", "protocol.py")
authentication = _load_module("authentication", "authentication.py")


class _Response:
    def __init__(self, status: int) -> None:
        self.status = status

    async def __aenter__(self) -> _Response:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


class _Session:
    def __init__(self, response: int | BaseException) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append({"url": url, **kwargs})
        if isinstance(self.response, BaseException):
            raise self.response
        return _Response(self.response)


class AuthenticationTests(unittest.IsolatedAsyncioTestCase):
    async def test_accepts_authorized_probe_responses(self) -> None:
        for status in (200, 404):
            with self.subTest(status=status):
                session = _Session(status)
                await authentication.async_validate_gateway_credentials(
                    session,
                    gateway_url="wss://gateway.example/ws/agent",
                    agent_id="hub-1",
                    token="secret-value",
                )
                self.assertEqual(len(session.calls), 1)
                self.assertEqual(
                    session.calls[0]["headers"],
                    {"Authorization": "Bearer secret-value"},
                )
                self.assertFalse(session.calls[0]["allow_redirects"])
                self.assertEqual(session.calls[0]["timeout"].total, 10)

    async def test_classifies_rejected_credentials_as_permanent(self) -> None:
        for status in (401, 403):
            with (
                self.subTest(status=status),
                self.assertRaises(authentication.InvalidAuthError),
            ):
                await authentication.async_validate_gateway_credentials(
                    _Session(status),
                    gateway_url="wss://gateway.example/ws/agent",
                    agent_id="hub-1",
                    token="secret-value",
                )

    async def test_classifies_server_and_transport_failures_as_retryable(self) -> None:
        for response in (500, _ClientError("offline"), TimeoutError()):
            with (
                self.subTest(response=response),
                self.assertRaises(authentication.CannotConnectError),
            ):
                await authentication.async_validate_gateway_credentials(
                    _Session(response),
                    gateway_url="wss://gateway.example/ws/agent",
                    agent_id="hub-1",
                    token="secret-value",
                )


if __name__ == "__main__":
    unittest.main()
