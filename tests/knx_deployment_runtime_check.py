"""Exercise the complete writer transaction with real HA KNX CONFIG_SCHEMA.

The file transaction, filtering and actual schema are real. KNX reload/state
boundaries use the controlled lifecycle fixture, so this sends no bus traffic.
"""

import asyncio
import importlib.util
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from homeassistant.components.knx import CONFIG_SCHEMA
from homeassistant.const import __version__
from xknx.telegram.address import GroupAddress

BASE = Path(__file__).parent
spec = importlib.util.spec_from_file_location(
    "real_schema_lifecycle", BASE / "test_knx_project_deployment.py"
)
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
m = fixture.m


def actual_schema(config):
    return CONFIG_SCHEMA({"knx": deepcopy(config)})["knx"]


async def run_case(label, manual, expected_count):
    case = fixture.DeploymentTests()
    case.setUp()
    case.writer.files.write("KNX/manual.yaml", manual)
    case.writer.files.write("configuration.yaml", b"knx: !include KNX/manual.yaml\n")
    case.writer._schema = actual_schema
    controlled_reload = case.reload

    async def reload(entry_id):
        result = await controlled_reload(entry_id)
        # Official KNX stores the schema-normalized config exactly once on setup.
        case.module.config_yaml = actual_schema(case.module.config_yaml)
        return result

    case.hass.config_entries.async_reload = reload
    try:
        with (
            patch.object(
                case.writer,
                "_compatibility",
                return_value=m.core_compatibility(__version__),
            ),
            patch.object(
                case.writer,
                "_address_format",
                return_value=GroupAddress.address_format.name,
            ),
        ):
            first = await case.writer.deploy(fixture.FINGERPRINT)
            assert first["status"] == (
                "completed" if expected_count == 2 else "partial"
            ), first
            assert case.writer.files.read("KNX/manual.yaml") == manual
            second = await case.writer.deploy(fixture.FINGERPRINT)
            assert second["loadedCount"] == expected_count, second
            assert case.reload_calls == 1
            case.writer.files.write_json(
                m.IMPORT_ASSOCIATION,
                {
                    "projectFingerprint": "b" * 64,
                    "parsedProjectDigest": m.project_digest(case.project),
                },
            )
            replaced = await case.writer.deploy("b" * 64)
            assert replaced["status"] == first["status"], replaced
            assert case.reload_calls == 2
            assert case.writer.files.read("KNX/manual.yaml") == manual
            assert (await case.writer.status("b" * 64))["loadedCount"] == expected_count
            print(
                "Real HA",
                __version__,
                "full writer transaction",
                label,
                "PASS",
            )
    finally:
        case.doCleanups()


async def run_manual_case():
    case = fixture.DeploymentTests()
    case.setUp()
    case.writer._schema = actual_schema
    controlled_reload = case.reload

    async def reload(entry_id):
        result = await controlled_reload(entry_id)
        case.module.config_yaml = actual_schema(case.module.config_yaml)
        return result

    case.hass.config_entries.async_reload = reload
    source = """knx:
  light:
    - name: Manual light
      address: "2/0/1"
  switch:
    - name: Manual switch
      address: "2/0/2"
  cover:
    - name: Manual cover
      move_long_address: "2/0/3"
  climate:
    - name: Manual climate
      temperature_address: "2/0/4"
      target_temperature_address: "2/0/5"
      target_temperature_state_address: "2/0/9"
  sensor:
    - name: Manual temperature
      state_address: "2/0/6"
      type: temperature
  binary_sensor:
    - name: Manual contact
      state_address: "2/0/7"
  scene:
    - name: Manual scene
      address: "2/0/8"
      scene_number: 2
"""
    try:
        with (
            patch.object(
                case.writer,
                "_compatibility",
                return_value=m.core_compatibility(__version__),
            ),
            patch.object(
                case.writer,
                "_address_format",
                return_value=GroupAddress.address_format.name,
            ),
        ):
            await case.writer.deploy(fixture.FINGERPRINT)
            checked = await case.writer.deploy(
                fixture.FINGERPRINT, manual_yaml=source, check_only=True
            )
            assert checked["status"] == "ready", checked
            assert case.reload_calls == 1
            result = await case.writer.deploy(
                fixture.FINGERPRINT, manual_yaml=source, baseline=checked["fingerprint"]
            )
            assert result["status"] == "completed", result
            assert result["manualCount"] == 7 and result["loadedCount"] == 9, result
            assert case.reload_calls == 2
            print("Real HA", __version__, "seven-platform manual YAML transaction PASS")
    finally:
        case.doCleanups()


async def main():
    await run_manual_case()
    cases = [
        (
            "manual list light + singleton climate",
            (
                b"# Preserve this file byte for byte.\n"
                b"light:\n  - name: Manual light\n    address: 2/0/1\n"
                b"climate:\n  name: Manual climate\n"
                b"  temperature_address: 5/0/1\n"
                b"  target_temperature_address: 5/0/2\n"
                b"  target_temperature_state_address: 5/0/3\n"
            ),
            2,
        ),
        (
            "singleton same-platform collision",
            b"light:\n  name: Manual\n  address: 1/0/1\n",
            1,
        ),
        (
            "singleton cross-platform collision",
            b"switch:\n  name: Manual\n  address: 1/0/1\n",
            1,
        ),
        (
            "nested RGB collision",
            (
                b"light:\n  - name: Manual RGB\n    individual_colors:\n"
                b"      red:\n        address: 1/0/1\n"
                b"        brightness_address: 2/0/1\n"
                b"      green:\n        brightness_address: 2/0/2\n"
                b"      blue:\n        brightness_address: 2/0/3\n"
            ),
            1,
        ),
    ]
    for label, manual, expected_count in cases:
        await run_case(label, manual, expected_count)


if __name__ == "__main__":
    asyncio.run(main())
