"""Real HA service, storage and WebSocket checks; no physical devices are touched."""

import asyncio
import importlib.util
import json
import logging
import sys
import tempfile
import types
from contextlib import suppress
from pathlib import Path
from unittest.mock import AsyncMock, patch

from homeassistant.auth.models import User
from homeassistant.components.websocket_api.connection import ActiveConnection
from homeassistant.const import __version__
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

BASE = Path(__file__).parents[1] / "custom_components/rexlite"
package = types.ModuleType("network_runtime_check")
package.__path__ = [str(BASE)]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location(
    package.__name__ + ".network_traffic_api", BASE / "network_traffic_api.py"
)
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)


async def main():
    with tempfile.TemporaryDirectory() as directory:
        hass = HomeAssistant(directory)
        if hasattr(dr, "async_setup"):
            dr.async_setup(hass)
        await dr.async_load(hass)
        await er.async_load(hass)
        # Reproduce HA's unchanged-report serialization cache using real States.
        deco_id = "device_tracker.quiet_deco"
        attrs = {"device_type": "client", "down_kilobytes_per_s": 0}
        hass.states.async_set(deco_id, "home", attrs)
        original = hass.states.get(deco_id)
        cached = original.as_dict_json
        hass.states.async_set(deco_id, "home", attrs)
        current = hass.states.get(deco_id)
        assert current is original and current.last_reported >= current.last_updated
        fake_registry = types.SimpleNamespace(
            entities={
                deco_id: types.SimpleNamespace(
                    entity_id=deco_id, platform="tplink_deco"
                )
            }
        )
        reports = m.deco_state_reports(hass, fake_registry)
        assert reports[0]["last_reported"] == current.last_reported.isoformat()
        assert reports[0]["attributes"]["down_kilobytes_per_s"] == 0
        assert json.loads(cached)["last_updated"] == current.last_updated.isoformat()
        manager = await m.async_register_network_traffic(hass)
        assert await m.async_register_network_traffic(hass) is manager
        admin = User(name="Admin", perm_lookup=None, is_owner=True)
        viewer = User(name="Viewer", perm_lookup=None)
        users = {admin.id: admin, viewer.id: viewer}
        hass.auth = types.SimpleNamespace(
            async_get_user=AsyncMock(side_effect=users.get)
        )
        for entity, value in [("sensor.rx", "1.25"), ("sensor.tx", "0.5")]:
            hass.states.async_set(entity, value, {"unit_of_measurement": "MB/s"})
        config = {
            "id": "port1",
            "name": "Zyxel port 1",
            "provider": "zyxel",
            "scope": "interface",
            "mode": "rate",
            "unit": "MB/s",
            "rx_entity": "sensor.rx",
            "tx_entity": "sensor.tx",
        }
        try:
            await hass.services.async_call(
                "rexlite",
                "configure_network_traffic",
                config,
                blocking=True,
                context=Context(user_id=viewer.id),
            )
        except Unauthorized:
            pass
        else:
            raise AssertionError("viewer configured telemetry")
        await hass.services.async_call(
            "rexlite",
            "configure_network_traffic",
            config,
            blocking=True,
            context=Context(user_id=admin.id),
        )
        restored = m.NetworkTraffic(hass, manager.store)
        await restored.load()
        assert restored.channels == manager.channels
        assert await hass.async_add_executor_job(
            Path(directory, ".storage", m.DATA_KEY).is_file
        )
        with patch.object(
            manager.store,
            "async_save",
            AsyncMock(side_effect=OSError("disk unavailable")),
        ):
            with suppress(OSError):
                await manager.configure({**config, "name": "changed"})
            assert manager.channels["port1"]["name"] == config["name"]
        responses = asyncio.Queue()

        def send(message):
            responses.put_nowait(
                json.loads(message) if isinstance(message, bytes | str) else message
            )

        options = (
            {"remote": None}
            if "remote" in ActiveConnection.__init__.__code__.co_varnames
            else {}
        )
        connection = ActiveConnection(
            logging.getLogger(__name__),
            hass,
            send,
            admin,
            types.SimpleNamespace(id="test"),
            **options,
        )
        connection.async_handle({"id": 1, "type": "rexlite/network_traffic"})
        result = await asyncio.wait_for(responses.get(), 5)
        assert result["success"], result
        row = result["result"]["channels"][0]
        assert row["scope"] == "interface" and row["deviceId"] == ""
        assert row["rxBitsPerSecond"] == 10_000_000
        assert row["txBitsPerSecond"] == 4_000_000
        connection.user = viewer
        connection.async_handle({"id": 2, "type": "rexlite/network_traffic"})
        assert (await asyncio.wait_for(responses.get(), 5))["error"][
            "code"
        ] == "unauthorized"
        await hass.services.async_call(
            "rexlite",
            "remove_network_traffic",
            {"id": "port1"},
            blocking=True,
            context=Context(user_id=admin.id),
        )
        await restored.load()
        assert not restored.channels
        connection.async_handle_close()
        await hass.async_stop(force=True)
    print(
        f"HA {__version__}: traffic services, persistence, write failure, "
        "WebSocket and admin access PASS"
    )


if __name__ == "__main__":
    asyncio.run(main())
