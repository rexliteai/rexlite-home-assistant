"""Execute against installed HA Core to verify real KNX package activation."""

import asyncio
import importlib.util
import sys
import tempfile
import types
from pathlib import Path

from homeassistant.components.knx import CONFIG_SCHEMA
from homeassistant.config import async_hass_config_yaml
from homeassistant.const import __version__
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_setup

base = Path(__file__).parents[1] / "custom_components/rexlite"
pkg = types.ModuleType("rexlite_real_writer")
pkg.__path__ = [str(base)]
sys.modules[pkg.__name__] = pkg
spec = importlib.util.spec_from_file_location(
    pkg.__name__ + ".knx_project_deployment", base / "knx_project_deployment.py"
)
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)


async def main():
    cases = {
        "new": "default_config:\n",
        "manual": "knx: !include KNX/manual.yaml\n",
        "packages_mapping": (
            "homeassistant:\n  packages:\n    original:\n"
            "      knx:\n        switch:\n          - name: Existing\n"
            "            address: 2/0/2\n"
        ),
        "packages_dir_named": (
            "homeassistant:\n  packages: !include_dir_named packages\n"
        ),
        "packages_dir_merged": (
            "homeassistant:\n  packages: !include_dir_merge_named packages\n"
        ),
    }
    for name, text in cases.items():
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "packages").mkdir()
            (root / "KNX").mkdir()
            (root / "KNX/manual.yaml").write_text(
                "light:\n  - name: Manual light\n    address: 2/0/1\n"
            )
            files = m.SafeFiles(root)
            files.write("configuration.yaml", text.encode())
            generated = (
                b"knx:\n  sensor:\n    - name: Temperature\n"
                b"      state_address: 1/0/1\n      type: temperature\n"
            )
            mode = m.core_compatibility(__version__)["identityMode"]
            if mode == "custom":
                generated += b"      unique_id: generated_temperature\n"
            files.write(m.GENERATED, generated)
            for relative, data in files.activation_changes().items():
                files.write(relative, data)
            hass = HomeAssistant(directory)
            async_setup(hass)
            resolved = await async_hass_config_yaml(hass)
            validated = CONFIG_SCHEMA(resolved)["knx"]
            expected_id = "generated_temperature" if mode == "custom" else "1/0/1"
            assert m.row_identity("sensor", validated["sensor"][0]) == expected_id, (
                name,
                validated,
            )
            if name == "manual":
                assert validated["light"][0]["name"] == "Manual light"
            if name == "packages_mapping":
                assert validated["switch"][0]["name"] == "Existing"
            print(name, "PASS", list(validated))
            await hass.async_stop(force=True)


if __name__ == "__main__":
    asyncio.run(main())
