"""Exercise registered commands through HA's real ActiveConnection dispatcher.

The deployment boundary is isolated: no host configuration or bus is changed.
Unlike a mock connection, ActiveConnection has no require_admin() method.
"""

import asyncio
import importlib.util
import inspect
import json
import logging
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import AsyncMock, patch

from homeassistant.auth.models import User
from homeassistant.components.websocket_api.connection import ActiveConnection
from homeassistant.const import __version__
from homeassistant.core import HomeAssistant

BASE = Path(__file__).parents[1] / "custom_components/rexlite"
package = types.ModuleType("rexlite_real_websocket")
package.__path__ = [str(BASE)]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location(
    package.__name__ + ".knx_project_deployment", BASE / "knx_project_deployment.py"
)
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)


async def main() -> None:
    fingerprint = "a" * 64
    commands = [
        ("project_capabilities", "capabilities", {}, ()),
        (
            "process_project",
            "process_project",
            {"file_id": "test-file", "password": "", "projectFingerprint": fingerprint},
            ("test-file", "", fingerprint),
        ),
        (
            "deploy_project",
            "deploy",
            {"projectFingerprint": fingerprint},
            (fingerprint,),
        ),
        (
            "project_deployment_status",
            "status",
            {"projectFingerprint": fingerprint},
            (fingerprint,),
        ),
    ]
    with tempfile.TemporaryDirectory() as directory:
        (Path(directory) / "configuration.yaml").write_text("default_config:\n")
        hass = HomeAssistant(directory)
        deployer = m.register_websocket_commands(hass)
        assert m.register_websocket_commands(hass) is deployer
        assert len(hass.data["websocket_api"]) == 4
        admin = User(name="Test administrator", perm_lookup=None, is_owner=True)
        viewer = User(name="Test viewer", perm_lookup=None)
        responses = asyncio.Queue()

        def send(message):
            responses.put_nowait(
                json.loads(message) if isinstance(message, bytes | str) else message
            )

        # Core 2026.5 added the remote argument to the connection constructor.
        connection_options = (
            {"remote": None}
            if "remote" in inspect.signature(ActiveConnection).parameters
            else {}
        )
        connection = ActiveConnection(
            logging.getLogger(__name__),
            hass,
            send,
            admin,
            types.SimpleNamespace(id="test-session"),
            **connection_options,
        )
        assert not hasattr(connection, "require_admin")
        message_id = 0

        async def request(command, fields):
            nonlocal message_id
            message_id += 1
            connection.async_handle(
                {"id": message_id, "type": f"rexlite/knx/{command}", **fields}
            )
            response = await asyncio.wait_for(responses.get(), 5)
            assert response["id"] == message_id, response
            return response

        # Exercise the real capability implementation before isolating mutations.
        response = await request("project_capabilities", {})
        assert response["success"] and response["result"]["supported"], response

        for user in (viewer, admin):
            connection.user = user
            for command, method, fields, arguments in commands:
                result = {"command": command}
                with patch.object(
                    deployer, method, AsyncMock(return_value=result)
                ) as work:
                    response = await request(command, fields)
                    if user is admin:
                        assert response["success"] and response["result"] == result, (
                            response
                        )
                        work.assert_awaited_once_with(*arguments)
                    else:
                        assert not response["success"], response
                        assert response["error"]["code"] == "unauthorized", response
                        work.assert_not_called()

        # Schema validation must reject bad fingerprints before any operation.
        with patch.object(deployer, "deploy", AsyncMock()) as work:
            response = await request(
                "deploy_project", {"projectFingerprint": "invalid"}
            )
            assert response["error"]["code"] == "invalid_format", response
            work.assert_not_called()

        # Failed imports remain explicit errors and a retry can succeed.
        fields = commands[1][2]
        for failure, expected in (
            (
                m.DeploymentError("project_file_fingerprint_mismatch"),
                "project_file_fingerprint_mismatch",
            ),
            (ValueError("private parser details"), "project_parse_failed"),
        ):
            with patch.object(
                deployer, "process_project", AsyncMock(side_effect=failure)
            ):
                response = await request("process_project", fields)
                assert response["error"] == {
                    "code": "project_import_failed",
                    "message": expected,
                }, response
        with patch.object(
            deployer, "process_project", AsyncMock(return_value={"ok": True})
        ):
            assert (await request("process_project", fields))["result"] == {"ok": True}

        connection.async_handle_close()
        await hass.async_stop(force=True)
    print(
        f"HA {__version__}: real WebSocket admin, denied access, "
        "schemas, errors and retry PASS"
    )


if __name__ == "__main__":
    asyncio.run(main())
