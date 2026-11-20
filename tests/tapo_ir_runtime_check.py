"""Run the Tapo IR command inside real Home Assistant.

- The WebSocket command, its schema and admin check, and the private store.
- When python-kasa is installed (CI installs each HA release's TP-Link
  requirement), the requests go through python-kasa's real SmartProtocol and
  KLAP transport, with only the network send replaced by a fake H110.
"""

import asyncio
import importlib.util
import json
import stat
import tempfile
from pathlib import Path
from types import SimpleNamespace

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant

TESTS = Path(__file__).parent
spec = importlib.util.spec_from_file_location("tapo_ir_unit", TESTS / "test_tapo_ir.py")
unit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(unit)
m = unit.m


def read_store(path: Path) -> tuple[str, int]:
    return path.read_text(encoding="utf-8"), stat.S_IMODE(path.stat().st_mode)


def rejected(schema, fields) -> bool:
    # HA 2026.9 validates with probatio, older releases with voluptuous: both
    # raise an "...Invalid" error.
    try:
        schema({"id": 1, "type": "rexlite/tapo_ir", **fields})
    except Exception as err:  # noqa: BLE001
        return type(err).__name__.endswith("Invalid")
    return False


class Connection:
    def __init__(self, admin=True):
        self.user = SimpleNamespace(is_admin=admin)
        self.results, self.errors = {}, {}

    def send_result(self, msg_id, result):
        self.results[msg_id] = result

    def send_error(self, msg_id, code, message):
        self.errors[msg_id] = (code, message)


async def call(hass, connection, handler, schema, msg_id, **fields):
    msg = schema({"id": msg_id, "type": "rexlite/tapo_ir", **fields})
    handler(hass, connection, msg)
    # async_response runs as a background task, which block_till_done skips.
    for _ in range(500):
        if msg_id in connection.results or msg_id in connection.errors:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError(f"no answer to {fields}")
    return connection.results.get(msg_id), connection.errors.get(msg_id)


async def check_home_assistant() -> None:
    with tempfile.TemporaryDirectory() as directory:
        hass = HomeAssistant(directory)
        device = unit.FakeH110()
        manager = m.register_tapo_ir(hass)
        assert m.register_tapo_ir(hass) is manager  # Idempotent across reloads.
        handler, schema = hass.data[websocket_api.DOMAIN]["rexlite/tapo_ir"]
        for bad in (
            {"action": "format"},
            {"action": "learn", "timeout": 3},
            {"action": "save_key", "kind": "car"},
            {"action": "ac", "temp": 40},
        ):
            assert rejected(schema, bad), f"schema accepted {bad}"
        assert not rejected(schema, {"action": "ac", "temp": 26, "fan": "auto"})

        guest = Connection(admin=False)
        try:
            handler(
                hass,
                guest,
                schema({"id": 1, "type": "rexlite/tapo_ir", "action": "hubs"}),
            )
        except Exception as err:  # noqa: BLE001 - HA raises Unauthorized
            assert type(err).__name__ == "Unauthorized", err
        else:
            raise AssertionError("a non-admin reached the command")

        connection = Connection()
        manager._kasa_loader = lambda: None
        result, _ = await call(hass, connection, handler, schema, 2, action="hubs")
        assert result == {"supported": False, "hubs": [], "found": []}, result

        manager._kasa_loaded = False
        manager._kasa_loader = lambda: unit.fake_kasa(device, [unit.raw_discovery()])
        result, _ = await call(
            hass, connection, handler, schema, 3, action="hubs", discover=True
        )
        assert [item["host"] for item in result["found"]] == ["192.168.68.65"], result
        _, error = await call(
            hass,
            connection,
            handler,
            schema,
            4,
            action="link",
            host="192.168.68.65",
            username=unit.ACCOUNT[0],
            password="wrong",
        )
        assert error[0] == "tapo_ir_auth", error
        result, _ = await call(
            hass,
            connection,
            handler,
            schema,
            5,
            action="link",
            host="192.168.68.65",
            username=unit.ACCOUNT[0],
            password=unit.ACCOUNT[1],
        )
        assert result["hub"]["id"] == unit.HUB, result
        stored, mode = await hass.async_add_executor_job(
            read_store, Path(directory, ".storage", m.STORE_KEY)
        )
        assert unit.ACCOUNT[1] not in stored and unit.ACCOUNT[0] not in stored
        assert unit.HASH in stored, stored
        assert mode == 0o600, oct(mode)

        m.LEARN_POLL_SECONDS = 0
        device.receive = [{"recv_status": 0, "pwm": 38000, "pulse": "9000,4500"}]
        result, _ = await call(
            hass,
            connection,
            handler,
            schema,
            6,
            action="learn",
            hub=unit.HUB,
            timeout=10,
        )
        # HA starts background tasks eagerly: a fast H110 may already be done.
        assert result["state"] in ("waiting", "captured"), result
        for _ in range(50):
            await asyncio.sleep(0)
            result, _ = await call(
                hass,
                connection,
                handler,
                schema,
                7,
                action="learn_status",
                hub=unit.HUB,
            )
            if result["state"] != "waiting":
                break
        assert result["state"] == "captured", result
        result, error = await call(
            hass,
            connection,
            handler,
            schema,
            8,
            action="save_key",
            hub=unit.HUB,
            captureId=result["captureId"],
            label="電源",
            name="客廳風扇",
            kind="fan",
        )
        assert error is None, error
        assert result["remote"]["name"] == "客廳風扇", result
        _, error = await call(
            hass,
            connection,
            handler,
            schema,
            9,
            action="send",
            hub=unit.HUB,
            remote="missing",
            key="power",
        )
        assert error[0] == "tapo_ir_failed", error
        await manager.close()
        await hass.async_stop(force=True)


async def check_python_kasa() -> None:
    kasa = m.load_kasa()
    if kasa is None:
        print("python-kasa is not installed here: KLAP protocol check skipped")
        return
    record = {
        "host": "192.0.2.10",
        "mac": unit.HUB,
        "connection": dict(m.DEFAULT_CONNECTION),
    }
    client = m.HubClient(kasa, record)
    client.use_credentials(*unit.ACCOUNT)
    protocol = client._ensure()
    assert type(protocol).__name__ == "SmartProtocol", type(protocol)
    assert type(protocol._transport).__name__ == "KlapTransportV2", type(
        protocol._transport
    )
    assert client.credentials_hash, "python-kasa gave no credentials hash"
    device = unit.FakeH110(host="192.0.2.10")

    async def send(request: str):
        payload = json.loads(request)
        method, params = payload["method"], payload.get("params")
        try:
            if method == "control_child":
                data = params["requestData"]
                result = device.child(
                    params["device_id"], data["method"], data["params"]
                )
            else:
                result = device.hub(method, params)
        except unit.DeviceError as err:
            return {"error_code": err.error_code}
        return {"error_code": 0, **({"result": result} if result is not None else {})}

    protocol._transport.send = send
    children = await client.children()
    assert [child["device_id"] for child in children] == ["TV01", "AC01"], children
    remote = await client.remote("TV01")
    assert remote["device_id"] == "TV01", remote
    await client.child("TV01", "sendIrCmdById", {"name": "power"}, batched=True)
    assert device.sent[-1] == ("TV01", "sendIrCmdById", {"name": "power"}), device.sent
    try:
        await client.hub("formatHub")
    except m.TapoIrError as err:
        assert err.code == "tapo_ir_firmware", err.code
    else:
        raise AssertionError("an unknown method did not fail")
    await client.close()
    await check_klap_without_account(kasa, record)


async def check_klap_without_account(kasa, record) -> None:
    """python-kasa's real KLAP handshake1 for a link without an account.

    An H110 that never joined a Tapo account (blank or TP-Link's setup
    credentials) lets it in; one bound to an account does not. Either way the
    H110 sees a single handshake1: python-kasa compares its challenge here, so
    trying without an account is no failed login on the device.
    """
    from kasa.credentials import DEFAULT_CREDENTIALS, get_default_credentials

    cases = (
        ("blank", kasa.Credentials(), True),
        ("setup", get_default_credentials(DEFAULT_CREDENTIALS["TAPO"]), True),
        ("bound", kasa.Credentials(*unit.ACCOUNT), False),
    )
    for name, device_credentials, opens in cases:
        client = m.HubClient(kasa, {**record, "credentials_hash": ""})
        transport = client._ensure()._transport
        assert transport._credentials == kasa.Credentials(), name
        assert transport.credentials_hash is None, name
        device_hash = transport.generate_auth_hash(device_credentials)
        posts: list[str] = []

        async def post(
            url, *, data=None, posts=posts, answer=device_hash, klap=transport, **_
        ):
            posts.append(str(url))
            remote_seed = bytes(range(16))
            return 200, remote_seed + klap.handshake1_seed_auth_hash(
                data, remote_seed, answer
            )

        transport._http_client.post = post
        try:
            _local, _remote, auth_hash = await transport.perform_handshake1()
        except kasa.AuthenticationError:
            assert not opens, f"{name}: python-kasa refused an H110 without an account"
        else:
            assert opens, f"{name}: python-kasa accepted an H110 bound to an account"
            assert auth_hash == device_hash, name
        assert [url.rsplit("/", 1)[-1] for url in posts] == ["handshake1"], posts
        await client.close()


async def main() -> None:
    await check_home_assistant()
    await check_python_kasa()
    print("Tapo IR runtime check passed")


if __name__ == "__main__":
    asyncio.run(main())
