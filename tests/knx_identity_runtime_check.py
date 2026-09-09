"""Check writer identities against real, offline HA KNX entity constructors.

Only the platform setup context is supplied; no device connection is started and
no state or control telegram is sent. Requires official HA and KNX requirements.
"""

import asyncio
import importlib
import importlib.util
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

from homeassistant.components.knx import CONFIG_SCHEMA
from homeassistant.components.knx import entity as knx_entity
from homeassistant.const import __version__
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from xknx import XKNX
from xknx.telegram.address import GroupAddress, GroupAddressType

BASE = Path(__file__).parents[1] / "custom_components/rexlite"
package = types.ModuleType("rexlite_real_identity")
package.__path__ = [str(BASE)]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location(
    package.__name__ + ".knx_project_deployment", BASE / "knx_project_deployment.py"
)
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)


async def main() -> None:
    samples = {
        "light": [{"name": "Light", "address": "1/0/1"}],
        "switch": [{"name": "Switch", "address": ["1/0/2", "1/0/3"]}],
        "sensor": [{"name": "Temperature", "state_address": "3/0/1", "type": "9.001"}],
        "binary_sensor": [{"name": "Contact", "state_address": ["3/0/2", "3/0/3"]}],
        "cover": [
            {
                "name": "Cover",
                "move_long_address": "2/0/1",
                "position_address": "2/0/3",
            },
            {"name": "Cover no position", "move_long_address": "2/0/4"},
        ],
        "scene": [{"name": "Scene", "address": "4/0/1", "scene_number": 13}],
        "climate": [
            {
                "name": "Climate",
                "temperature_address": "5/0/1",
                "target_temperature_state_address": "5/0/2",
                "target_temperature_address": "5/0/3",
            }
        ],
    }
    validated = CONFIG_SCHEMA({"knx": samples})["knx"]
    with tempfile.TemporaryDirectory() as directory:
        hass = HomeAssistant(directory)
        if hasattr(dr, "async_setup"):
            dr.async_setup(hass)
        await dr.async_load(hass)
        await er.async_load(hass)
        module = types.SimpleNamespace(hass=hass, xknx=XKNX(), connected=False)
        count = 0
        legacy = m.core_compatibility(__version__)["identityMode"] == "legacy"
        original_format = GroupAddress.address_format
        try:
            for address_format in (
                GroupAddressType.LONG,
                GroupAddressType.SHORT,
                GroupAddressType.FREE,
            ):
                GroupAddress.address_format = address_format
                for platform, configs in validated.items():
                    if not configs:
                        continue
                    component = importlib.import_module(
                        f"homeassistant.components.knx.{platform}"
                    )
                    class_name = "KnxYaml" + "".join(
                        word.title() for word in platform.split("_")
                    )
                    constructor = getattr(component, class_name)
                    with patch.object(
                        knx_entity,
                        "async_get_current_platform",
                        return_value=types.SimpleNamespace(domain=platform),
                        create=True,
                    ):
                        for config in configs:
                            migrated_entity_id = None
                            if (
                                not legacy
                                and platform == "light"
                                and address_format == GroupAddressType.FREE
                            ):
                                registry = er.async_get(hass)
                                migrated_entity_id = registry.async_get_or_create(
                                    "light",
                                    "knx",
                                    "2049",
                                    suggested_object_id="original_light",
                                ).entity_id
                            entity = constructor(module, config)
                            expected = m.row_identity(
                                platform,
                                config,
                                address_format.name if legacy else None,
                            )
                            if migrated_entity_id:
                                assert (
                                    registry.async_get_entity_id(
                                        "light", "knx", expected
                                    )
                                    == migrated_entity_id
                                )
                            assert entity.unique_id == expected, (
                                platform,
                                address_format,
                                entity.unique_id,
                                expected,
                            )
                            count += 1
            print(
                "Real HA KNX native entity identities: PASS",
                count,
                "constructors across 3 address formats",
            )
        finally:
            GroupAddress.address_format = original_format
            await hass.async_stop(force=True)


if __name__ == "__main__":
    asyncio.run(main())
