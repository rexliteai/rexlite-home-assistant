"""Run the HomeKit listing on real python-zeroconf records and HA's tables."""

import ast
import asyncio
import importlib.util
import json
import socket
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import homeassistant
from homeassistant.components import websocket_api
from homeassistant.components import zeroconf as ha_zeroconf
from homeassistant.core import HomeAssistant
from zeroconf import DNSAddress, DNSCache, DNSPointer, DNSService, DNSText

PATH = Path(__file__).parents[1] / "custom_components/rexlite/homekit_accessories.py"
spec = importlib.util.spec_from_file_location("homekit_runtime", PATH)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
HA = Path(homeassistant.__file__).parent

TYPE_A, TYPE_AAAA, TYPE_PTR, TYPE_SRV, TYPE_TXT = 1, 28, 12, 33, 16
CLASS_IN, CLASS_UNIQUE = 1, 0x8000


def txt(**values) -> bytes:
    items = [f"{key}={value}".encode() for key, value in values.items()]
    return b"".join(bytes([len(item)]) + item for item in items)


def service(service_type, name, server, port, address, **properties):
    full = f"{name}.{service_type}"
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    unique = CLASS_IN | CLASS_UNIQUE
    return [
        DNSPointer(service_type, TYPE_PTR, CLASS_IN, 4500, full),
        DNSService(full, TYPE_SRV, unique, 120, 0, 0, port, server),
        DNSText(full, TYPE_TXT, unique, 4500, txt(**properties)),
        DNSAddress(
            server,
            TYPE_AAAA if family == socket.AF_INET6 else TYPE_A,
            unique,
            120,
            socket.inet_pton(family, address),
        ),
    ]


def assert_defines(relative: str, names: list[str]) -> None:
    """HA source still defines or re-exports these (imported lazily at runtime)."""
    tree = ast.parse((HA / relative).read_text(encoding="utf-8"))
    found = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    } | {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    missing = set(names) - found
    assert not missing, f"{relative} no longer provides {sorted(missing)}"


def check_home_assistant_sources() -> None:
    assert_defines(
        "components/bluetooth/__init__.py",
        ["async_scanner_count", "async_discovered_service_info"],
    )
    assert_defines("components/thread/__init__.py", ["async_get_preferred_dataset"])
    assert_defines("components/thread/discovery.py", ["async_read_zeroconf_cache"])
    flow = ast.parse(
        (HA / "components/homekit_controller/config_flow.py").read_text(
            encoding="utf-8"
        )
    )
    ignored = next(
        ast.literal_eval(node.value)
        for node in flow.body
        if isinstance(node, ast.Assign)
        and any(
            getattr(target, "id", "") == "HOMEKIT_IGNORE" for target in node.targets
        )
    )
    assert set(ignored) == m.IGNORED_MODELS, ignored
    manifest = json.loads(
        (HA / "components/homekit_controller/manifest.json").read_text(encoding="utf-8")
    )
    assert {"_hap._tcp.local.", "_hap._udp.local."} <= set(manifest["zeroconf"])
    assert {"manufacturer_id": 76, "manufacturer_data_start": [6]} in manifest[
        "bluetooth"
    ], manifest["bluetooth"]


async def main() -> None:
    check_home_assistant_sources()

    cache = DNSCache()
    cache.async_add_records(
        service(
            "_hap._tcp.local.",
            "Eve Energy 50FF",
            "Eve-Energy-50FF.local.",
            51826,
            "192.168.68.80",
            id="1A:2B:3C:4D:5E:6F",
            md="Eve Energy 20EBO8301",
            ci=7,
            sf=1,
        )
        + service(
            "_hap._udp.local.",
            "Eve Door 9A7B",
            "Eve-Door-9A7B.local.",
            5683,
            "fd11:22:33:44::5",
            id="AA:BB:CC:DD:EE:01",
            md="Eve Door 20EBN9901",
            ci=10,
            sf=0,
        )
        + service(
            "_hap._tcp.local.",
            "Hue Bridge 4F2A",
            "Hue-Bridge-4F2A.local.",
            8080,
            "192.168.68.90",
            id="AA:BB:CC:DD:EE:02",
            md="BSB002",
            ci=2,
            sf=0,
        )
    )
    aiozc = SimpleNamespace(zeroconf=SimpleNamespace(cache=cache))

    with tempfile.TemporaryDirectory() as directory:
        hass = HomeAssistant(directory)
        hass.config_entries = SimpleNamespace(
            async_entries=lambda domain: [],
            flow=SimpleNamespace(
                async_progress_by_handler=lambda handler: [
                    {
                        "flow_id": "flow-eve",
                        "context": {
                            "source": "zeroconf",
                            "unique_id": "1a:2b:3c:4d:5e:6f",
                        },
                    }
                ]
            ),
        )
        with patch.object(
            ha_zeroconf, "async_get_async_instance", AsyncMock(return_value=aiozc)
        ):
            _, rows = await m.zeroconf_rows(hass)
            by_id = {row["id"]: row for row in rows}
            eve = by_id["1a:2b:3c:4d:5e:6f"]
            assert eve["name"] == "Eve Energy 50FF", eve
            assert (eve["host"], eve["port"], eve["category"]) == (
                "192.168.68.80",
                51826,
                7,
            ), eve
            assert eve["transport"] == "ip" and not eve["paired"] and eve["lastSeen"]
            door = by_id["aa:bb:cc:dd:ee:01"]
            assert (door["transport"], door["host"], door["paired"]) == (
                "thread",
                "fd11:22:33:44::5",
                True,
            ), door

            with patch(
                "homeassistant.loader.async_get_custom_components",
                AsyncMock(return_value={}),
            ):
                resolve = await m.native_resolver(hass)
                assert resolve("BSB002") == "hue"
                assert resolve("LIFX A19") is None  # Offered to HomeKit Device too.
                assert resolve("Eve Energy 20EBO8301") is None

                original = m.reachable

                async def answering(host, port):
                    return host in {"192.168.68.80", "192.168.68.90"}

                with patch.object(
                    m, "reachable", lambda items: original(items, probe=answering)
                ):
                    result = await m.homekit_accessories(hass)

        listed = [
            (item["name"], item["status"], item["flowId"], item["nativeDomain"])
            for item in result["accessories"]
        ]
        assert listed == [
            ("Eve Energy 50FF", "ready", "flow-eve", ""),
            ("Eve Door 9A7B", "paired_elsewhere", "", ""),
            ("Hue Bridge 4F2A", "native", "", "hue"),
        ], listed
        assert isinstance(result["bluetooth"]["adapters"], int)
        thread = result["thread"]
        assert isinstance(thread["routers"], int) and thread["routers"] >= 0, thread
        assert isinstance(thread["preferredNetwork"], bool), thread
        assert thread["routes"] is None or thread["routes"] >= 0, thread
        assert result["scannedAt"]

        m.register_homekit_accessories(hass)
        m.register_homekit_accessories(hass)  # Idempotent across config entry reloads.
        commands = hass.data.get(websocket_api.DOMAIN, {})
        assert "rexlite/homekit/accessories" in commands, sorted(commands)
        await hass.async_stop(force=True)

    print("HomeKit accessory listing runtime check passed")


if __name__ == "__main__":
    asyncio.run(main())
