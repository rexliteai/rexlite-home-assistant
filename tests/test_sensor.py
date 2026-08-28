"""Connection-status sensor tests without a Home Assistant installation."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

PACKAGE_PATH = Path(__file__).parents[1] / "custom_components" / "rexlite"
PACKAGE_NAME = "rexlite_sensor_test"


class _SensorEntity:
    """Stub Home Assistant sensor entity."""


class _REXLiTEEntity:
    """Stub integration entity base."""

    def __init__(self, coordinator: Any) -> None:
        self.coordinator = coordinator


package = types.ModuleType(PACKAGE_NAME)
package.__path__ = [str(PACKAGE_PATH)]
package.REXLiTEConfigEntry = object
sys.modules[PACKAGE_NAME] = package

homeassistant_components = types.ModuleType("homeassistant.components")
homeassistant_sensor = types.ModuleType("homeassistant.components.sensor")
homeassistant_sensor.SensorEntity = _SensorEntity
homeassistant_core = types.ModuleType("homeassistant.core")
homeassistant_core.HomeAssistant = object
homeassistant_entity_platform = types.ModuleType(
    "homeassistant.helpers.entity_platform"
)
homeassistant_entity_platform.AddEntitiesCallback = object
sys.modules["homeassistant.components"] = homeassistant_components
sys.modules["homeassistant.components.sensor"] = homeassistant_sensor
sys.modules["homeassistant.core"] = homeassistant_core
sys.modules["homeassistant.helpers.entity_platform"] = homeassistant_entity_platform

entity_module = types.ModuleType(f"{PACKAGE_NAME}.entity")
entity_module.REXLiTEEntity = _REXLiTEEntity
sys.modules[f"{PACKAGE_NAME}.entity"] = entity_module

spec = importlib.util.spec_from_file_location(
    f"{PACKAGE_NAME}.sensor",
    PACKAGE_PATH / "sensor.py",
)
if spec is None or spec.loader is None:
    raise RuntimeError("Unable to load REXLiTE sensor platform")
sensor = importlib.util.module_from_spec(spec)
sys.modules[f"{PACKAGE_NAME}.sensor"] = sensor
spec.loader.exec_module(sensor)


class _RuntimeState:
    def __init__(
        self,
        *,
        connected: bool,
        remote_admin_enabled: bool,
        authentication_failed: bool,
    ) -> None:
        self.connected = connected
        self.remote_admin_enabled = remote_admin_enabled
        self.authentication_failed = authentication_failed

    @property
    def cloud_service_state(self) -> str:
        if self.authentication_failed:
            return "verification_required"
        if not self.connected:
            return "disconnected"
        if not self.remote_admin_enabled:
            return "ready_for_activation"
        return "connected"


class SensorTests(unittest.TestCase):
    def test_connected_state(self) -> None:
        entity = self._entity(
            connected=True,
            remote_admin_enabled=True,
            authentication_failed=False,
        )

        self.assertEqual(entity.native_value, "connected")

    def test_disconnected_state(self) -> None:
        entity = self._entity(
            connected=False,
            remote_admin_enabled=True,
            authentication_failed=False,
        )

        self.assertEqual(entity.native_value, "disconnected")

    def test_preconfigured_health_connection_is_ready_for_later_activation(
        self,
    ) -> None:
        entity = self._entity(
            connected=True,
            remote_admin_enabled=False,
            authentication_failed=False,
        )

        self.assertEqual(entity.native_value, "ready_for_activation")

    def test_authentication_failure_takes_priority(self) -> None:
        entity = self._entity(
            connected=False,
            remote_admin_enabled=False,
            authentication_failed=True,
        )

        self.assertEqual(entity.native_value, "verification_required")

    @staticmethod
    def _entity(
        *,
        connected: bool,
        remote_admin_enabled: bool,
        authentication_failed: bool,
    ) -> Any:
        coordinator = SimpleNamespace(
            config=SimpleNamespace(agent_id="hub-1"),
            data=_RuntimeState(
                connected=connected,
                remote_admin_enabled=remote_admin_enabled,
                authentication_failed=authentication_failed,
            ),
        )
        return sensor.REXLiTEConnectionStatusSensor(coordinator)


if __name__ == "__main__":
    unittest.main()
