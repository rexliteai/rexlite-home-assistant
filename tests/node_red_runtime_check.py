"""Run the Node-RED bridge registration inside real Home Assistant.

- `rexlite/nodered/*` WebSocket commands, their admin check and private store.
- A job saved before a restart is answered by the first `rexlite/nodered/job`
  call (on site, 11/17: 「找不到部署作業」 after HA restarted).
- Once HA has started, the palette module install is scheduled.
"""

import asyncio
import importlib.util
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant

TESTS = Path(__file__).parent
spec = importlib.util.spec_from_file_location(
    "node_red_bridge_unit", TESTS / "test_node_red_bridge.py"
)
unit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(unit)
m = unit.m
JOB = "3f2a3c8e-5d1b-4a8e-9c3d-2b1a0f9e8d7c"


class Connection:
    def __init__(self, admin=True):
        self.user = SimpleNamespace(is_admin=admin)
        self.results, self.errors = {}, {}

    def send_result(self, msg_id, result):
        self.results[msg_id] = result

    def send_error(self, msg_id, code, message):
        self.errors[msg_id] = (code, message)


async def call(hass, connection, command, msg_id, **fields):
    handler, schema = hass.data[websocket_api.DOMAIN][command]
    handler(hass, connection, schema({"id": msg_id, "type": command, **fields}))
    # async_response runs as a background task, which block_till_done skips.
    for _ in range(500):
        if msg_id in connection.results or msg_id in connection.errors:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError(f"no answer to {command}")
    return connection.results.get(msg_id), connection.errors.get(msg_id)


async def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        storage = Path(directory) / ".storage"
        storage.mkdir()
        saved = {
            "endpoint": {"mode": "auto"},
            "bridges": {},
            "jobs": [
                {
                    "jobId": JOB,
                    "kind": "deploy",
                    "state": "failed",
                    "stage": "modules",
                    "error": {"code": "node_red_install_failed", "message": "x"},
                }
            ],
        }
        (storage / m.STORE_KEY).write_text(
            json.dumps({"version": 1, "key": m.STORE_KEY, "data": saved}),
            encoding="utf-8",
        )
        hass = HomeAssistant(directory)
        # No HTTP here; a bare HA has no network/zeroconf for its shared session.
        with patch(
            "homeassistant.helpers.aiohttp_client.async_get_clientsession",
            lambda hass: SimpleNamespace(),
        ):
            bridges = m.register_node_red_bridge(hass)
        assert m.register_node_red_bridge(hass) is bridges  # Once per HA instance.
        for command in ("status", "deploy", "job", "remove", "scan"):
            assert f"rexlite/nodered/{command}" in hass.data[websocket_api.DOMAIN]

        # First call after the restart: the stored job, not "job not found".
        result, error = await call(
            hass, Connection(), "rexlite/nodered/job", 1, jobId=JOB
        )
        assert error is None, error
        assert (result["state"], result["error"]["code"]) == (
            "failed",
            "node_red_install_failed",
        ), result
        _result, error = await call(
            hass, Connection(), "rexlite/nodered/job", 2, jobId=JOB.replace("3", "4")
        )
        assert error and error[0] == "node_red_job_not_found", error
        try:
            await call(
                hass, Connection(admin=False), "rexlite/nodered/job", 3, jobId=JOB
            )
        except Exception as err:  # noqa: BLE001 - HA raises Unauthorized
            assert type(err).__name__ == "Unauthorized", err
        else:
            raise AssertionError("a non-admin reached the command")

        # HA started → the hub's Node-RED gets its modules without a wizard.
        scheduled: list[float] = []
        bridges.schedule_prepare = lambda delay=m.PREPARE_DELAY_S: scheduled.append(
            delay
        )
        await hass.async_start()
        await hass.async_block_till_done()
        assert scheduled == [m.PREPARE_DELAY_S], scheduled
        await hass.async_stop(force=True)
    print("Node-RED bridge runtime check passed")


if __name__ == "__main__":
    asyncio.run(main())
