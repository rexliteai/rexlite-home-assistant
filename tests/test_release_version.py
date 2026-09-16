"""Prevent stale firmware labels after a package release."""

import ast
import json
import unittest
from pathlib import Path


class ReleaseVersionTests(unittest.TestCase):
    def test_device_firmware_matches_manifest(self) -> None:
        root = Path(__file__).parents[1] / "custom_components" / "rexlite"
        manifest = json.loads((root / "manifest.json").read_text())
        tree = ast.parse((root / "const.py").read_text())
        versions = [
            ast.literal_eval(node.value)
            for node in tree.body
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "INTEGRATION_VERSION"
        ]
        self.assertEqual(versions, [manifest["version"]])
