"""The cloud tunnel must load whatever happens to other integrations.

Home Assistant sets REXLiTE AI up only after every integration named under
``after_dependencies`` that is loading too, and refuses to set it up when one
of them fails (invalid KNX YAML, a broken MQTT or backup setup). The hub then
shows the config entry as not loaded and the tunnel that would repair it never
starts. So the manifest names none, and the code looks those integrations up
when a request needs them instead of importing them statically: hassfest
rejects a static import of an integration the manifest does not name.
"""

from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

PACKAGE = Path(__file__).parents[1] / "custom_components" / "rexlite"
# Helpers hassfest lets any integration import (script/hassfest/dependencies.py).
ALWAYS_ALLOWED = {"webhook", "websocket_api"}


def static_references(source: str) -> set[str]:
    """Integrations a file imports the way hassfest's collector sees them."""

    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("homeassistant.components."):
                found.add(node.module.split(".")[2])
            elif node.module == "homeassistant.components":
                found.update(name.name for name in node.names)
        elif isinstance(node, ast.Import):
            found.update(
                name.name.split(".")[2]
                for name in node.names
                if name.name.startswith("homeassistant.components.")
            )
    return found


class ManifestDependencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = json.loads((PACKAGE / "manifest.json").read_text())

    def test_no_integration_can_hold_back_the_tunnel(self) -> None:
        self.assertNotIn("after_dependencies", self.manifest)
        # Small core helpers only; each one is needed for the tunnel's commands.
        self.assertEqual(
            self.manifest["dependencies"], ["file_upload", "webhook", "websocket_api"]
        )

    def test_other_integrations_are_never_imported_statically(self) -> None:
        allowed = ALWAYS_ALLOWED | set(self.manifest["dependencies"])
        for path in sorted(PACKAGE.glob("*.py")):
            # A platform file (sensor.py, backup.py) may import its own platform.
            platforms = {item.stem for item in PACKAGE.glob("*.py")}
            unexpected = (
                static_references(path.read_text(encoding="utf-8"))
                - allowed
                - platforms
            )
            self.assertEqual(unexpected, set(), path.name)

    def test_the_collector_sees_every_import_form(self) -> None:
        source = (
            "import homeassistant.components.hue as hue\n"
            "def f():\n"
            "    from homeassistant.components import mqtt, knx\n"
            "    from homeassistant.components.thread.discovery import x\n"
        )
        self.assertEqual(static_references(source), {"hue", "mqtt", "knx", "thread"})


if __name__ == "__main__":
    unittest.main()
