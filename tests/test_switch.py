"""Cloud-service switch lifecycle tests without Home Assistant dependencies."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

PACKAGE_PATH = Path(__file__).parents[1] / "custom_components" / "rexlite"
PACKAGE_NAME = "rexlite_switch_test"


class _SwitchEntity:
    """Stub Home Assistant switch entity."""


class _REXLiTEEntity:
    """Stub integration entity base."""

    def __init__(self, coordinator: Any) -> None:
        self.coordinator = coordinator


package = types.ModuleType(PACKAGE_NAME)
package.__path__ = [str(PACKAGE_PATH)]
package.REXLiTEConfigEntry = object
sys.modules[PACKAGE_NAME] = package

homeassistant_components = types.ModuleType("homeassistant.components")
homeassistant_switch = types.ModuleType("homeassistant.components.switch")
homeassistant_switch.SwitchEntity = _SwitchEntity
homeassistant_core = types.ModuleType("homeassistant.core")
homeassistant_core.HomeAssistant = object
homeassistant_entity_platform = types.ModuleType(
    "homeassistant.helpers.entity_platform"
)
homeassistant_entity_platform.AddEntitiesCallback = object
sys.modules["homeassistant.components"] = homeassistant_components
sys.modules["homeassistant.components.switch"] = homeassistant_switch
sys.modules["homeassistant.core"] = homeassistant_core
sys.modules["homeassistant.helpers.entity_platform"] = homeassistant_entity_platform

const_module = types.ModuleType(f"{PACKAGE_NAME}.const")
const_module.CONF_REMOTE_ADMIN_ENABLED = "remote_admin_enabled"
sys.modules[f"{PACKAGE_NAME}.const"] = const_module
entity_module = types.ModuleType(f"{PACKAGE_NAME}.entity")
entity_module.REXLiTEEntity = _REXLiTEEntity
sys.modules[f"{PACKAGE_NAME}.entity"] = entity_module

spec = importlib.util.spec_from_file_location(
    f"{PACKAGE_NAME}.switch",
    PACKAGE_PATH / "switch.py",
)
if spec is None or spec.loader is None:
    raise RuntimeError("Unable to load REXLiTE switch platform")
switch = importlib.util.module_from_spec(spec)
sys.modules[f"{PACKAGE_NAME}.switch"] = switch
spec.loader.exec_module(switch)


class _Coordinator:
    def __init__(self, events: list[tuple[str, Any]]) -> None:
        self.config = SimpleNamespace(agent_id="factory-preconfigured-hub")
        self.data = SimpleNamespace(remote_admin_enabled=False)
        self.events = events

    async def async_set_remote_admin(self, enabled: bool) -> None:
        self.events.append(("runtime", enabled))
        self.data.remote_admin_enabled = enabled


class _ConfigEntries:
    def __init__(self, events: list[tuple[str, Any]]) -> None:
        self.events = events

    def async_update_entry(self, entry: Any, *, options: dict[str, Any]) -> None:
        self.events.append(("persist", options))
        entry.options = options


class SwitchTests(unittest.IsolatedAsyncioTestCase):
    async def test_later_activation_is_persisted_before_runtime_restart(self) -> None:
        events: list[tuple[str, Any]] = []
        coordinator = _Coordinator(events)
        entry = SimpleNamespace(
            runtime_data=coordinator,
            options={"preserved_option": "value"},
        )
        entity = switch.REXLiTERemoteAdminSwitch(entry)
        entity.hass = SimpleNamespace(config_entries=_ConfigEntries(events))

        await entity.async_turn_on()

        self.assertEqual(
            events,
            [
                (
                    "persist",
                    {"preserved_option": "value", "remote_admin_enabled": True},
                ),
                ("runtime", True),
            ],
        )
        self.assertTrue(entity.is_on)
        self.assertTrue(entry.options["remote_admin_enabled"])


if __name__ == "__main__":
    unittest.main()
