"""Tapo H110 IR remotes: helpers, linking, learning and verified remote changes."""

import asyncio
import base64
import importlib.util
import json
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

PATH = Path(__file__).parents[1] / "custom_components/rexlite/tapo_ir.py"
spec = importlib.util.spec_from_file_location("tapo_ir_test", PATH)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

ACCOUNT = ("nick@example.com", "correct horse")
HASH = "hash-of-nick"


def b64(text):
    return base64.b64encode(text.encode()).decode()


# --- a fake python-kasa and H110 ----------------------------------------------


class KasaException(Exception):
    pass


class DeviceError(KasaException):
    def __init__(self, message="", error_code=None):
        super().__init__(message)
        self.error_code = error_code


class AuthenticationError(DeviceError):
    pass


class Credentials:
    def __init__(self, username, password):
        self.username, self.password = username, password


class DeviceConfig:
    def __init__(self, host, timeout=None, connection_type=None):
        self.host, self.timeout, self.connection_type = host, timeout, connection_type
        self.credentials = None
        self.credentials_hash = None


class ConnectionParameters:
    @staticmethod
    def from_dict(value):
        return SimpleNamespace(**value)


def remote(device_id, name, keys=(), model="Custom", **extra):
    return {
        "device_id": device_id,
        "category": "ir.remote",
        "device_type": "SMART.TAPOREMOTE",
        "nickname": b64(name),
        "model": model,
        "avatar": "remote",
        "frequency": 38000,
        "key_list": [deepcopy(key) for key in keys],
        **extra,
    }


def key(name, label, order, pwm=38000, pulse="9000,4500,560"):
    return {
        "name": name,
        "display_name": b64(label),
        "order": order,
        "pwm": pwm,
        "pulse": pulse,
    }


class FakeH110:
    """The H110 behind one IP, speaking python-kasa's query() contract."""

    def __init__(self, host="192.168.68.65", model="H110(EU)"):
        self.host = host
        self.model = model
        self.mac = "84-47-09-63-92-C8"
        self.children = [
            remote("TV01", "客廳電視", [key("power", "", 1, pulse="1,2,3")]),
            remote(
                "AC01",
                "臥室冷氣",
                model="AC",
                ac_status="P1_M0_T26_S0_D0",
            ),
        ]
        self.receive = []
        self.calls = []
        self.sent = []
        self.next_id = 1
        self.fail = {}
        self.rename_new_keys = False
        # False: never joined a Tapo account, so python-kasa's blank/setup
        # credentials (tried for any login) get in.
        self.bound = True

    def check(self, config):
        if config.host != self.host:
            raise KasaException("no route to host")
        if not self.bound:
            return
        if config.credentials is not None:
            if (config.credentials.username, config.credentials.password) != ACCOUNT:
                raise AuthenticationError("handshake failed", error_code=-1501)
        elif config.credentials_hash != HASH:
            raise AuthenticationError("handshake failed", error_code=-1501)

    def find(self, device_id):
        for child in self.children:
            if child["device_id"] == device_id:
                return child
        return None

    def hub(self, method, params):
        if failure := self.fail.pop(method, None):
            raise failure
        if method == "get_device_info":
            return {
                "device_id": "HUB",
                "type": "SMART.TAPOHUB",
                "model": self.model,
                "mac": self.mac,
                "nickname": b64("客廳 H110"),
            }
        if method == "get_child_device_list":
            return {
                "child_device_list": deepcopy(self.children),
                "start_index": 0,
                "sum": len(self.children),
            }
        if method == "addIrRemoteDevice":
            device_id = f"NEW{self.next_id}"
            self.next_id += 1
            self.children.append(
                remote(device_id, base64.b64decode(params["nickname"]).decode())
            )
            return {"device_id": device_id}
        if method in ("startIrReceiveMode", "stopIrReceiveMode"):
            return None
        if method == "getIrReceiveStatus":
            return self.receive.pop(0) if self.receive else {"recv_status": 1}
        raise DeviceError("unknown", error_code=-1002)

    def child(self, device_id, method, params):
        if failure := self.fail.pop(method, None):
            raise failure
        child = self.find(device_id)
        if child is None:
            return {"responseData": {"error_code": -1001}}
        if method == "get_device_info":
            return {"responseData": {"result": deepcopy(child), "error_code": 0}}
        if method == "setKeyInfo":
            names = {item["name"] for item in params["delete_key_list"]}
            child["key_list"] = [k for k in child["key_list"] if k["name"] not in names]
            for edit in params["edit_key_list"]:
                saved = {k: v for k, v in edit.items() if k != "id"}
                existing = [k for k in child["key_list"] if k["name"] == edit["name"]]
                if existing:
                    existing[0].update(saved)
                else:
                    if self.rename_new_keys:
                        saved["name"] = f"hub_{len(child['key_list']) + 1}"
                    child["key_list"].append(saved)
            return {"responseData": {"result": {}, "error_code": 0}}
        if method == "setDeviceInfo":
            child["nickname"] = params["nickname"]
            return {"responseData": {"error_code": 0}}
        if method == "deleteRemote":
            self.children.remove(child)
            return {"responseData": {"error_code": 0}}
        if method == "multipleRequest":
            responses = []
            for request in params["requests"]:
                self.sent.append((device_id, request["method"], request.get("params")))
                if request["method"] == "sendIrCmdByStatus":
                    p = request["params"]
                    child["ac_status"] = (
                        f"P{int(p['power'])}_M{p['mode']}_T{p['temp']}"
                        f"_S{p['wind_speed']}_D{p['wind_direct']}"
                    )
                responses.append(
                    {"method": request["method"], "result": {}, "error_code": 0}
                )
            return {
                "responseData": {"result": {"responses": responses}, "error_code": 0}
            }
        return {"responseData": {"error_code": -1002}}


class FakeProtocol:
    def __init__(self, device, config):
        self.device, self.config = device, config
        self._transport = SimpleNamespace(
            credentials_hash=HASH
            if config.credentials is not None
            else config.credentials_hash
        )
        self.closed = False

    async def query(self, request, retry_count=3):
        self.device.check(self.config)
        ((method, params),) = request.items()
        self.device.calls.append((method, params, retry_count))
        if method == "control_child":
            data = params["requestData"]
            return {
                "control_child": self.device.child(
                    params["device_id"], data["method"], data["params"]
                )
            }
        return {method: self.device.hub(method, params)}

    async def close(self):
        self.closed = True


def fake_kasa(device, discovered=None):
    async def discover(target, discovery_timeout, on_discovered_raw):
        for raw in discovered or []:
            on_discovered_raw(raw)
        return {}

    return SimpleNamespace(
        AuthenticationError=AuthenticationError,
        Credentials=Credentials,
        DeviceConfig=DeviceConfig,
        DeviceConnectionParameters=ConnectionParameters,
        DeviceError=DeviceError,
        Discover=SimpleNamespace(discover=discover),
        KasaException=KasaException,
        get_protocol=lambda config: FakeProtocol(device, config),
    )


def raw_discovery(host="192.168.68.65", model="H110(EU)", family="SMART.TAPOHUB"):
    return {
        "meta": {"ip": host, "port": 20002},
        "discovery_response": {
            "error_code": 0,
            "result": {
                "device_type": family,
                "device_model": model,
                "ip": host,
                "mac": "84-47-09-63-92-C8",
                "mgt_encrypt_schm": {
                    "is_support_https": False,
                    "encrypt_type": "KLAP",
                    "http_port": 80,
                    "lv": 2,
                },
            },
        },
    }


class MemoryStore:
    def __init__(self, data=None):
        self.data = data
        self.saves = 0

    async def async_load(self):
        return deepcopy(self.data)

    async def async_save(self, data):
        self.data = deepcopy(data)
        self.saves += 1


def manager(device, *, store=None, discovered=None, kasa=True):
    created = []

    def create_task(job):
        task = asyncio.ensure_future(job)
        created.append(task)
        return task

    instance = m.TapoIrManager(
        store or MemoryStore(),
        kasa_loader=(lambda: fake_kasa(device, discovered)) if kasa else (lambda: None),
        create_task=create_task,
    )
    instance.tasks = created
    return instance


async def linked(device, **options):
    instance = manager(device, **options)
    await instance.handle(
        {
            "action": "link",
            "host": device.host,
            "username": ACCOUNT[0],
            "password": ACCOUNT[1],
        }
    )
    return instance


HUB = "84:47:09:63:92:c8"


# --- helpers -------------------------------------------------------------------


class HelperTests(unittest.TestCase):
    def test_names_decode_base64_and_keep_plain_text(self):
        self.assertEqual(m.decode_text(b64("客廳電視")), "客廳電視")
        self.assertEqual(m.decode_text("Living TV"), "Living TV")
        self.assertEqual(m.decode_text(""), "")

    def test_mac_host_and_account_are_normalised(self):
        self.assertEqual(m.normalize_mac("84-47-09-63-92-C8"), HUB)
        self.assertEqual(m.normalize_mac("nope"), "")
        self.assertEqual(m.valid_host(" 192.168.68.65 "), "192.168.68.65")
        self.assertEqual(m.valid_host("h110.local"), "")
        self.assertEqual(m.mask_account("nick@example.com"), "n•••@example.com")

    def test_discovery_keeps_only_ir_hubs(self):
        candidate = m.discovery_candidate(raw_discovery())
        self.assertEqual(candidate["mac"], HUB)
        self.assertEqual(candidate["model"], "H110")
        self.assertEqual(candidate["connection"]["encryption_type"], "KLAP")
        self.assertEqual(candidate["connection"]["login_version"], 2)
        self.assertIsNone(m.discovery_candidate(raw_discovery(model="H100(EU)")))
        self.assertIsNone(m.discovery_candidate(raw_discovery(family="SMART.TAPOPLUG")))
        self.assertIsNone(m.discovery_candidate({"meta": {}}))

    def test_key_labels_prefer_the_given_name(self):
        self.assertEqual(
            m.key_label({"name": "custom_key", "display_name": b64("電源")}, 1), "電源"
        )
        self.assertEqual(m.key_label({"name": "VOL+"}, 2), "Volume Up")
        self.assertEqual(m.key_label({"name": "power"}, 3), "Power")
        self.assertEqual(m.key_label({"name": "custom_key"}, 4), "按鍵 4")

    def test_summary_hides_waveforms_and_orders_keys(self):
        child = remote(
            "TV01",
            "客廳電視",
            [key("custom_b", "音量＋", 2), key("custom_a", "電源", 1)],
        )
        summary = m.remote_summary(child, "tv")
        self.assertEqual(
            [item["label"] for item in summary["keys"]], ["電源", "音量＋"]
        )
        self.assertNotIn("pulse", json.dumps(summary))
        self.assertEqual(summary["kind"], "tv")
        self.assertTrue(summary["learned"])
        self.assertIsNone(summary["ac"])

    def test_ac_state_parsing_and_changes(self):
        state = m.parse_ac_status({"ac_status": "P1_M0_T26_S0_D1"})
        self.assertEqual(state, {"P": 1, "M": 0, "T": 26, "S": 0, "D": 1})
        view = m.ac_view(state)
        self.assertEqual(
            (view["power"], view["mode"], view["temp"], view["fan"]),
            (True, "cool", 26, "auto"),
        )
        changed = m.apply_ac_changes(state, {"temp": 24, "fan": "high", "mode": "heat"})
        self.assertEqual(
            (changed["T"], changed["S"], changed["M"], changed["P"]), (24, 3, 1, 1)
        )
        with self.assertRaises(m.TapoIrError) as error:
            m.apply_ac_changes({"P": 1}, {"temp": 25})
        self.assertEqual(error.exception.code, "tapo_ir_ac_state")
        for bad in ({"temp": 31}, {"temp": True}, {"mode": "dry"}, {"fan": "turbo"}):
            with self.assertRaises(m.TapoIrError):
                m.apply_ac_changes(state, bad)

    def test_child_envelopes_check_every_code(self):
        request = m.child_request(
            "TV01", "sendIrCmdById", {"name": "power"}, batched=True
        )
        self.assertEqual(
            request["control_child"]["requestData"]["params"]["requests"][0],
            {"method": "sendIrCmdById", "params": {"name": "power"}},
        )
        ok = {
            "control_child": {
                "responseData": {
                    "result": {
                        "responses": [
                            {"method": "sendIrCmdById", "result": {}, "error_code": 0}
                        ]
                    },
                    "error_code": 0,
                }
            }
        }
        self.assertEqual(m.child_result(ok, "sendIrCmdById"), {})
        refused = deepcopy(ok)
        refused["control_child"]["responseData"]["result"]["responses"][0][
            "error_code"
        ] = -1002
        with self.assertRaises(m.TapoIrError) as error:
            m.child_result(refused, "sendIrCmdById")
        self.assertEqual(error.exception.code, "tapo_ir_firmware")
        with self.assertRaises(m.TapoIrError) as error:
            m.child_result(
                {"control_child": {"responseData": {"error_code": -1501}}}, "x"
            )
        self.assertEqual(error.exception.code, "tapo_ir_auth")

    def test_new_key_names_never_collide(self):
        keys = [{"name": "custom_key"}, {"name": "custom_key_2"}]
        self.assertEqual(m.new_key_name(keys, "電源"), "custom_key_3")
        self.assertEqual(m.new_key_name(keys, "Vol+"), "custom_vol_plus")


# --- the manager ---------------------------------------------------------------


class ManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._poll, self._min = m.LEARN_POLL_SECONDS, m.LEARN_MIN
        m.LEARN_POLL_SECONDS = 0

    def tearDown(self):
        m.LEARN_POLL_SECONDS, m.LEARN_MIN = self._poll, self._min

    async def test_without_python_kasa_the_command_says_so(self):
        instance = manager(FakeH110(), kasa=False)
        self.assertEqual(
            await instance.handle({"action": "hubs"}),
            {"supported": False, "hubs": [], "found": []},
        )
        with self.assertRaises(m.TapoIrError) as error:
            await instance.handle(
                {
                    "action": "link",
                    "host": "192.168.68.65",
                    "username": "a",
                    "password": "b",
                }
            )
        self.assertEqual(error.exception.code, "tapo_ir_unsupported")

    async def test_link_keeps_only_the_hash_and_a_masked_account(self):
        device, store = FakeH110(), MemoryStore()
        instance = manager(device, store=store, discovered=[raw_discovery()])
        found = await instance.handle({"action": "hubs", "discover": True})
        self.assertEqual([item["host"] for item in found["found"]], ["192.168.68.65"])
        result = await instance.handle(
            {
                "action": "link",
                "host": "192.168.68.65",
                "username": ACCOUNT[0],
                "password": ACCOUNT[1],
            }
        )
        self.assertEqual(result["hub"]["id"], HUB)
        self.assertEqual(result["hub"]["name"], "客廳 H110")
        self.assertEqual(result["hub"]["account"], "n•••@example.com")
        self.assertEqual(
            [item["name"] for item in result["remotes"]], ["客廳電視", "臥室冷氣"]
        )
        saved = json.dumps(store.data, ensure_ascii=False)
        self.assertIn(HASH, saved)
        self.assertNotIn(ACCOUNT[1], saved)
        self.assertNotIn(ACCOUNT[0], saved)
        hubs = await instance.handle({"action": "hubs", "discover": True})
        self.assertEqual([hub["id"] for hub in hubs["hubs"]], [HUB])
        self.assertEqual(hubs["found"], [])

    async def test_wrong_password_and_wrong_device_store_nothing(self):
        store = MemoryStore()
        instance = manager(FakeH110(), store=store)
        with self.assertRaises(m.TapoIrError) as error:
            await instance.handle(
                {
                    "action": "link",
                    "host": "192.168.68.65",
                    "username": ACCOUNT[0],
                    "password": "nope",
                }
            )
        self.assertEqual(error.exception.code, "tapo_ir_auth")
        instance = manager(FakeH110(model="H100(EU)"), store=store)
        with self.assertRaises(m.TapoIrError) as error:
            await instance.handle(
                {
                    "action": "link",
                    "host": "192.168.68.65",
                    "username": ACCOUNT[0],
                    "password": ACCOUNT[1],
                }
            )
        self.assertEqual(error.exception.code, "tapo_ir_invalid")
        self.assertIsNone(store.data)

    async def test_a_later_session_logs_in_with_the_stored_hash(self):
        device, store = FakeH110(), MemoryStore()
        await linked(device, store=store)
        restarted = manager(device, store=store)
        result = await restarted.handle({"action": "remotes", "hub": HUB})
        self.assertEqual(len(result["remotes"]), 2)
        tv = next(item for item in result["remotes"] if item["id"] == "TV01")
        self.assertEqual(tv["keys"], [{"name": "power", "label": "Power", "order": 1}])
        ac = next(item for item in result["remotes"] if item["id"] == "AC01")
        self.assertEqual(
            (ac["kind"], ac["ac"]["temp"], ac["ac"]["complete"]), ("ac", 26, True)
        )

    async def test_an_unbound_h110_links_without_an_account(self):
        device, store = FakeH110(), MemoryStore()
        device.bound = False
        instance = manager(device, store=store, discovered=[raw_discovery()])
        hubs = await instance.handle({"action": "hubs", "discover": True})
        self.assertIs(hubs["accountOptional"], True)
        result = await instance.handle({"action": "link", "host": device.host})
        self.assertEqual((result["hub"]["id"], result["hub"]["account"]), (HUB, ""))
        self.assertEqual(len(result["remotes"]), 2)
        record = store.data["hubs"][HUB]
        self.assertEqual((record["credentials_hash"], record["account"]), ("", ""))
        # After a restart the session again goes without credentials.
        restarted = manager(device, store=store)
        listed = await restarted.handle({"action": "remotes", "hub": HUB})
        self.assertEqual(len(listed["remotes"]), 2)
        config = restarted._clients[HUB]._protocol.config
        self.assertEqual((config.credentials, config.credentials_hash), (None, None))

    async def test_a_bound_h110_asks_for_the_account(self):
        device, store = FakeH110(), MemoryStore()
        instance = manager(device, store=store)
        with self.assertRaises(m.TapoIrError) as error:
            await instance.handle({"action": "link", "host": device.host})
        self.assertEqual(error.exception.code, "tapo_ir_account_required")
        self.assertIsNone(store.data)
        # Half an account is a typo, not a link without one.
        for fields in ({"username": ACCOUNT[0]}, {"password": ACCOUNT[1]}):
            with self.assertRaises(m.TapoIrError) as error:
                await instance.handle({"action": "link", "host": device.host, **fields})
            self.assertEqual(error.exception.code, "tapo_ir_invalid")
        result = await instance.handle(
            {
                "action": "link",
                "host": device.host,
                "username": ACCOUNT[0],
                "password": ACCOUNT[1],
            }
        )
        self.assertEqual(result["hub"]["account"], "n•••@example.com")

    async def test_a_hub_linked_without_an_account_says_when_it_needs_one(self):
        device = FakeH110()
        device.bound = False
        instance = manager(device)
        await instance.handle({"action": "link", "host": device.host})
        device.bound = True  # it joined a Tapo account in the Tapo App
        with self.assertRaises(m.TapoIrError) as error:
            await instance.handle({"action": "remotes", "hub": HUB})
        self.assertEqual(error.exception.code, "tapo_ir_account_required")

    async def test_learning_captures_one_button_and_always_stops_receiving(self):
        device = FakeH110()
        instance = await linked(device)
        device.receive = [
            {"recv_status": 1},
            {"recv_status": 0, "pwm": 38000, "pulse": "9000,4500"},
        ]
        started = await instance.handle({"action": "learn", "hub": HUB, "timeout": 20})
        self.assertEqual(started["state"], "waiting")
        again = await instance.handle({"action": "learn", "hub": HUB})
        self.assertEqual(again["state"], "waiting")
        await asyncio.gather(*instance.tasks)
        status = await instance.handle({"action": "learn_status", "hub": HUB})
        self.assertEqual(status["state"], "captured")
        self.assertTrue(status["captureId"])
        methods = [call[0] for call in device.calls]
        self.assertEqual(methods.count("startIrReceiveMode"), 1)
        self.assertEqual(methods.count("stopIrReceiveMode"), 1)

    async def test_learning_reports_timeouts_and_unusable_signals(self):
        device = FakeH110()
        instance = await linked(device)
        m.LEARN_MIN = 0
        await instance.handle({"action": "learn", "hub": HUB, "timeout": 0})
        await asyncio.gather(*instance.tasks)
        self.assertEqual(
            (await instance.handle({"action": "learn_status", "hub": HUB}))["state"],
            "timeout",
        )
        device.receive = [{"recv_status": -2}]
        await instance.handle({"action": "learn", "hub": HUB, "timeout": 5})
        await asyncio.gather(*instance.tasks)
        status = await instance.handle({"action": "learn_status", "hub": HUB})
        self.assertEqual(status["state"], "failed")
        self.assertIn("無法使用", status["error"])
        self.assertEqual([c[0] for c in device.calls].count("stopIrReceiveMode"), 2)

    async def test_learning_can_be_stopped_and_blocks_other_changes(self):
        device = FakeH110()
        instance = await linked(device)
        device.receive = [{"recv_status": 1}] * 1000
        await instance.handle({"action": "learn", "hub": HUB, "timeout": 30})
        with self.assertRaises(m.TapoIrError) as error:
            await instance.handle(
                {"action": "send", "hub": HUB, "remote": "TV01", "key": "power"}
            )
        self.assertEqual(error.exception.code, "tapo_ir_busy")
        stopped = await instance.handle({"action": "learn_stop", "hub": HUB})
        self.assertEqual(stopped["state"], "stopped")
        self.assertEqual([c[0] for c in device.calls].count("stopIrReceiveMode"), 1)

    async def capture(self, instance, device, pulse="9000,4500,560"):
        device.receive = [{"recv_status": 0, "pwm": 38000, "pulse": pulse}]
        await instance.handle({"action": "learn", "hub": HUB, "timeout": 10})
        await asyncio.gather(*instance.tasks)
        return (await instance.handle({"action": "learn_status", "hub": HUB}))[
            "captureId"
        ]

    async def test_a_new_remote_is_created_with_its_first_button(self):
        device, store = FakeH110(), MemoryStore()
        instance = await linked(device, store=store)
        capture_id = await self.capture(instance, device)
        result = await instance.handle(
            {
                "action": "save_key",
                "hub": HUB,
                "captureId": capture_id,
                "label": "電源",
                "name": "客廳風扇",
                "kind": "fan",
            }
        )
        created = result["remote"]
        self.assertEqual(
            (created["name"], created["kind"], created["learned"]),
            ("客廳風扇", "fan", True),
        )
        self.assertEqual([item["label"] for item in created["keys"]], ["電源"])
        child = device.find(created["id"])
        self.assertEqual(child["key_list"][0]["pulse"], "9000,4500,560")
        self.assertEqual(store.data["kinds"][created["id"]], "fan")
        add = next(call for call in device.calls if call[0] == "addIrRemoteDevice")
        self.assertEqual(add[1]["category"], "ir.remote")
        self.assertEqual(add[1]["model"], "Custom")
        self.assertEqual(add[2], 0)
        with self.assertRaises(m.TapoIrError) as error:
            await instance.handle(
                {
                    "action": "save_key",
                    "hub": HUB,
                    "captureId": capture_id,
                    "label": "電源",
                    "name": "再一次",
                }
            )
        self.assertEqual(error.exception.code, "tapo_ir_capture_expired")

    async def test_a_failed_first_button_removes_the_new_remote(self):
        device = FakeH110()
        instance = await linked(device)
        capture_id = await self.capture(instance, device)
        device.fail["setKeyInfo"] = DeviceError("busy", error_code=-1301)
        with self.assertRaises(m.TapoIrError):
            await instance.handle(
                {
                    "action": "save_key",
                    "hub": HUB,
                    "captureId": capture_id,
                    "label": "電源",
                    "name": "風扇",
                }
            )
        self.assertEqual(
            [child["device_id"] for child in device.children], ["TV01", "AC01"]
        )

    async def test_buttons_are_added_relearned_and_deleted_with_read_back(self):
        device = FakeH110()
        instance = await linked(device)
        capture_id = await self.capture(instance, device, pulse="1,1,1")
        result = await instance.handle(
            {
                "action": "save_key",
                "hub": HUB,
                "remote": "TV01",
                "captureId": capture_id,
                "label": "音量＋",
            }
        )
        self.assertEqual(
            [item["label"] for item in result["remote"]["keys"]], ["Power", "音量＋"]
        )
        capture_id = await self.capture(instance, device, pulse="7,7,7")
        result = await instance.handle(
            {
                "action": "save_key",
                "hub": HUB,
                "remote": "TV01",
                "key": "power",
                "captureId": capture_id,
                "label": "電源",
            }
        )
        self.assertEqual(device.find("TV01")["key_list"][0]["pulse"], "7,7,7")
        self.assertEqual(
            result["remote"]["keys"][0], {"name": "power", "label": "電源", "order": 1}
        )
        result = await instance.handle(
            {"action": "delete_key", "hub": HUB, "remote": "TV01", "key": "power"}
        )
        self.assertEqual(
            [item["label"] for item in result["remote"]["keys"]], ["音量＋"]
        )

    async def test_a_key_the_hub_renamed_is_still_verified(self):
        device = FakeH110()
        device.rename_new_keys = True
        instance = await linked(device)
        capture_id = await self.capture(instance, device)
        result = await instance.handle(
            {
                "action": "save_key",
                "hub": HUB,
                "remote": "TV01",
                "captureId": capture_id,
                "label": "靜音",
            }
        )
        self.assertIn("靜音", [item["label"] for item in result["remote"]["keys"]])

    async def test_rename_delete_and_send(self):
        device = FakeH110()
        instance = await linked(device)
        renamed = await instance.handle(
            {
                "action": "rename",
                "hub": HUB,
                "remote": "TV01",
                "name": "主臥電視",
                "kind": "tv",
            }
        )
        self.assertEqual(
            (renamed["remote"]["name"], renamed["remote"]["kind"]), ("主臥電視", "tv")
        )
        await instance.handle(
            {"action": "send", "hub": HUB, "remote": "TV01", "key": "power"}
        )
        self.assertEqual(device.sent[-1], ("TV01", "sendIrCmdById", {"name": "power"}))
        send = [call for call in device.calls if call[0] == "control_child"][-1]
        self.assertEqual(send[2], 0)
        await instance.handle({"action": "delete_remote", "hub": HUB, "remote": "TV01"})
        self.assertIsNone(device.find("TV01"))
        with self.assertRaises(m.TapoIrError) as error:
            await instance.handle(
                {"action": "delete_remote", "hub": HUB, "remote": "TV01"}
            )
        self.assertEqual(error.exception.code, "tapo_ir_not_found")

    async def test_air_conditioners_send_a_complete_state(self):
        device = FakeH110()
        instance = await linked(device)
        result = await instance.handle(
            {"action": "ac", "hub": HUB, "remote": "AC01", "temp": 24, "fan": "low"}
        )
        self.assertEqual(device.sent[-1][1], "sendIrCmdByStatus")
        self.assertEqual(
            device.sent[-1][2],
            {
                "power": True,
                "on": True,
                "mode": 0,
                "temp": 24,
                "wind_speed": 1,
                "wind_direct": 0,
            },
        )
        self.assertEqual(
            (result["remote"]["ac"]["temp"], result["remote"]["ac"]["fan"]), (24, "low")
        )
        await instance.handle(
            {"action": "ac", "hub": HUB, "remote": "AC01", "power": False}
        )
        self.assertFalse(device.sent[-1][2]["power"])
        with self.assertRaises(m.TapoIrError):
            await instance.handle(
                {"action": "ac", "hub": HUB, "remote": "TV01", "temp": 24}
            )

    async def test_a_hub_with_a_new_address_is_found_again(self):
        device, store = FakeH110(), MemoryStore()
        await linked(device, store=store)
        device.host = "192.168.68.90"
        instance = manager(
            device, store=store, discovered=[raw_discovery(host="192.168.68.90")]
        )
        result = await instance.handle({"action": "remotes", "hub": HUB})
        self.assertEqual(result["hub"]["host"], "192.168.68.90")
        self.assertEqual(store.data["hubs"][HUB]["host"], "192.168.68.90")

    async def test_an_expired_session_is_retried_once_for_writes(self):
        device = FakeH110()
        instance = await linked(device)
        device.fail["multipleRequest"] = DeviceError("expired", error_code=-40401)
        await instance.handle(
            {"action": "send", "hub": HUB, "remote": "TV01", "key": "power"}
        )
        self.assertEqual(len(device.sent), 1)

    async def test_unlink_forgets_the_hub(self):
        device, store = FakeH110(), MemoryStore()
        instance = await linked(device, store=store)
        await instance.handle({"action": "unlink", "hub": HUB})
        self.assertEqual(store.data["hubs"], {})
        with self.assertRaises(m.TapoIrError) as error:
            await instance.handle({"action": "remotes", "hub": HUB})
        self.assertEqual(error.exception.code, "tapo_ir_not_linked")


if __name__ == "__main__":
    unittest.main()
