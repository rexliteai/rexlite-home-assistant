"""Diagnostics privacy regression tests without Home Assistant dependencies."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

PACKAGE_PATH = Path(__file__).parents[1] / "custom_components" / "rexlite"
PACKAGE_NAME = "rexlite_diagnostics_test"

package = types.ModuleType(PACKAGE_NAME)
package.__path__ = [str(PACKAGE_PATH)]
package.REXLiTEConfigEntry = object
sys.modules[PACKAGE_NAME] = package

homeassistant = types.ModuleType("homeassistant")
homeassistant_const = types.ModuleType("homeassistant.const")


class _Platform:
    SENSOR = "sensor"
    BINARY_SENSOR = "binary_sensor"
    SWITCH = "switch"


homeassistant_const.Platform = _Platform
homeassistant_core = types.ModuleType("homeassistant.core")
homeassistant_core.HomeAssistant = object
homeassistant.const = homeassistant_const
sys.modules["homeassistant"] = homeassistant
sys.modules["homeassistant.const"] = homeassistant_const
sys.modules["homeassistant.core"] = homeassistant_core


def _load_module(name: str, filename: str) -> types.ModuleType:
    module_name = f"{PACKAGE_NAME}.{name}"
    spec = importlib.util.spec_from_file_location(module_name, PACKAGE_PATH / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


const = _load_module("const", "const.py")
diagnostics = _load_module("diagnostics", "diagnostics.py")


class DiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_redacts_credentials_identity_and_local_address(self) -> None:
        entry = SimpleNamespace(
            data={
                const.CONF_AGENT_ID: "ipc-sensitive-identifier",
                const.CONF_AGENT_AUTH_TOKEN: "test-token-placeholder",
                const.CONF_HOME_ASSISTANT_URL: "http://192.0.2.54:8123",
                const.CONF_GATEWAY_WS_URL: "wss://gateway.example/ws/agent",
            },
            options={const.CONF_REMOTE_ADMIN_ENABLED: True},
            runtime_data=SimpleNamespace(
                data=SimpleNamespace(
                    connected=False,
                    remote_admin_enabled=True,
                    access_mode="full_control",
                    reconnect_attempt=0,
                    last_connected_at=None,
                    last_error="authentication_failed (HTTP 401)",
                    authentication_failed=True,
                    cloud_service_state="verification_required",
                )
            ),
        )

        result = await diagnostics.async_get_config_entry_diagnostics(None, entry)

        self.assertEqual(result["entry"]["data"][const.CONF_AGENT_ID], "**REDACTED**")
        self.assertEqual(
            result["entry"]["data"][const.CONF_AGENT_AUTH_TOKEN], "**REDACTED**"
        )
        self.assertEqual(
            result["entry"]["data"][const.CONF_HOME_ASSISTANT_URL], "**REDACTED**"
        )
        self.assertEqual(
            result["entry"]["data"][const.CONF_GATEWAY_WS_URL],
            "wss://gateway.example/ws/agent",
        )
        self.assertTrue(result["runtime"]["authentication_failed"])
        self.assertEqual(
            result["runtime"]["cloud_service_state"], "verification_required"
        )


if __name__ == "__main__":
    unittest.main()
