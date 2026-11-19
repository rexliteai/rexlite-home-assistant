"""HomeKit accessory listing: parsing, pairing state, Thread routes and ordering."""

import asyncio
import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

PATH = Path(__file__).parents[1] / "custom_components/rexlite/homekit_accessories.py"
spec = importlib.util.spec_from_file_location("homekit_accessories_test", PATH)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def service(name="Eve Energy 50FF._hap._tcp.local.", **properties):
    props = {b"id": b"1A:2B:3C:4D:5E:6F", b"md": b"Eve Energy 20EBO8301"}
    props |= {b"ci": b"7", b"sf": b"1"}
    props |= {key.encode(): value for key, value in properties.items()}
    return SimpleNamespace(
        name=name,
        type="_hap._tcp.local.",
        properties=props,
        port=51826,
        parsed_addresses=lambda: ["fe80::1", "192.168.68.80"],
    )


def advertisement(status_flags=0x01, device_id=b"\xaa\xbb\xcc\xdd\xee\xff", acid=10):
    return (
        bytes([0x06, 0x31, status_flags])
        + device_id
        + acid.to_bytes(2, "little")
        + (b"\x01\x00\x02\x02\x12\x34\x56\x78")
    )


def row(**changes):
    return {
        "id": "1a:2b:3c:4d:5e:6f",
        "name": "Eve Energy 50FF",
        "model": "Eve Energy 20EBO8301",
        "category": 7,
        "transport": "ip",
        "host": "192.168.68.80",
        "port": 51826,
        "paired": False,
        "lastSeen": "",
        **changes,
    }


class ParsingTests(unittest.TestCase):
    def test_device_ids_are_normalized_or_rejected(self):
        self.assertEqual(m.normalize_id(b"1A:2B:3C:4D:5E:6F"), "1a:2b:3c:4d:5e:6f")
        self.assertEqual(m.normalize_id(" aa:bb:cc:dd:ee:ff "), "aa:bb:cc:dd:ee:ff")
        for value in ("1A:2B:3C:4D:5E", "zz:2b:3c:4d:5e:6f", "1a2b3c4d5e6f", None, b""):
            self.assertEqual(m.normalize_id(value), "", value)

    def test_status_flag_bit_zero_means_unpaired(self):
        self.assertFalse(m.is_paired(0x01))
        self.assertFalse(m.is_paired(0x03))
        self.assertTrue(m.is_paired(0x00))
        self.assertTrue(m.is_paired(0x02))  # Wi-Fi not configured, still owned.

    def test_lan_host_prefers_private_ipv4_then_unique_local_ipv6(self):
        self.assertEqual(
            m.lan_host(["fe80::1", "fd11::5", "192.168.68.80"]), "192.168.68.80"
        )
        self.assertEqual(m.lan_host(["fe80::1%en0", "fd11::5"]), "fd11::5")
        for addresses in (
            ["8.8.8.8"],
            ["2a00:1450::1"],
            ["127.0.0.1", "::1"],
            ["169.254.1.1", "fe80::1"],
            ["224.0.0.251", "0.0.0.0"],
            ["not-an-address"],
            [],
        ):
            self.assertEqual(m.lan_host(addresses), "", addresses)

    def test_cached_service_becomes_a_row(self):
        result = m.service_row(service(), "ip", 1763560800000)
        self.assertEqual(result["id"], "1a:2b:3c:4d:5e:6f")
        self.assertEqual(result["name"], "Eve Energy 50FF")
        self.assertEqual(result["model"], "Eve Energy 20EBO8301")
        self.assertEqual(result["category"], 7)
        self.assertEqual(result["host"], "192.168.68.80")
        self.assertEqual(result["port"], 51826)
        self.assertFalse(result["paired"])
        self.assertTrue(result["lastSeen"].startswith("2025-11-19T"))

    def test_paired_service_and_upper_case_keys(self):
        info = service(sf=b"0")
        info.properties = {key.upper(): value for key, value in info.properties.items()}
        result = m.service_row(info, "thread")
        self.assertTrue(result["paired"])
        self.assertEqual(result["model"], "Eve Energy 20EBO8301")
        self.assertEqual(result["transport"], "thread")
        self.assertEqual(result["lastSeen"], "")

    def test_service_without_id_is_skipped_and_name_falls_back_to_model(self):
        self.assertIsNone(m.service_row(service(id=b"nope"), "ip"))
        result = m.service_row(service(name="._hap._tcp.local."), "ip")
        self.assertEqual(result["name"], "Eve Energy 20EBO8301")

    def test_missing_flags_and_category_default_to_unpaired_other(self):
        info = service()
        del info.properties[b"sf"], info.properties[b"ci"]
        result = m.service_row(info, "ip")
        self.assertFalse(result["paired"])
        self.assertEqual(result["category"], 1)

    def test_ble_advertisement_layout(self):
        parsed = m.ble_advertisement({76: advertisement(0x01, acid=10)})
        self.assertEqual(
            parsed, {"id": "aa:bb:cc:dd:ee:ff", "statusFlags": 1, "category": 10}
        )
        self.assertIsNone(m.ble_advertisement({76: b"\x10\x05" + b"\x00" * 20}))
        self.assertIsNone(m.ble_advertisement({76: advertisement()[:10]}))
        self.assertIsNone(m.ble_advertisement({6: advertisement()}))

    def test_ble_rows_keep_fresh_advertisements_only(self):
        fresh = SimpleNamespace(
            manufacturer_data={76: advertisement(0x00)},
            name="Eve Door 1234",
            address="AA:BB:CC:DD:EE:FF",
            time=995.0,
        )
        result = m.ble_row(fresh, 1000.0, 1763560800.0)
        self.assertEqual(result["transport"], "ble")
        self.assertTrue(result["paired"])
        self.assertEqual(result["name"], "Eve Door 1234")
        self.assertTrue(result["lastSeen"].startswith("2025-11-19T"))
        stale = SimpleNamespace(**{**fresh.__dict__, "time": 1000.0 - 301})
        self.assertIsNone(m.ble_row(stale, 1000.0, 1763560800.0))
        unnamed = SimpleNamespace(**{**fresh.__dict__, "name": "aa:bb:cc:dd:ee:ff"})
        self.assertEqual(m.ble_row(unnamed, 1000.0, 1763560800.0)["name"], "")


class StatusTests(unittest.TestCase):
    def test_precedence(self):
        entries = {"1a:2b:3c:4d:5e:6f": "entry-1"}
        self.assertEqual(
            m.accessory_status(row(paired=True), entries, None),
            ("added", "", "entry-1"),
        )
        # A reset accessory with a stale entry is offered again by HA.
        self.assertEqual(m.accessory_status(row(), entries, None), ("ready", "", ""))
        self.assertEqual(
            m.accessory_status(row(model="T8400"), {}, None), ("ignored", "", "")
        )
        self.assertEqual(
            m.accessory_status(row(paired=True, model="BSB002"), {}, "hue"),
            ("native", "hue", ""),
        )
        self.assertEqual(
            m.accessory_status(row(paired=True), {}, None), ("paired_elsewhere", "", "")
        )
        self.assertEqual(m.accessory_status(row(), {}, None), ("ready", "", ""))


ROUTER_A = "fe800000000000000c8a2bfffe3d4e5f"
ROUTER_B = "fe800000000000001111222233334444"
ON_LINK = "0" * 32


def route(destination, prefix_length, next_hop, flags):
    """One /proc/net/ipv6_route line: destination … next hop … flags, device."""
    fields = [destination, prefix_length, "0" * 32, "00", next_hop]
    return " ".join([*fields, "00000400", "00000000", "00000000", flags, "eth0"])


ROUTES = "\n".join(
    [
        route("fd4a9a3b0c1d00000000000000000000", "40", ROUTER_A, "00450003"),
        route("fd4a9a3b0c1d00000000000000000000", "40", ROUTER_B, "00450003"),
        route("fd77000000000000000000000000000a", "40", ON_LINK, "00000001"),
        route("fd99000000000000000000000000000b", "30", ROUTER_A, "00450003"),
        route("fe800000000000000000000000000000", "40", ON_LINK, "00000001"),
        "broken line",
    ]
)


class ThreadRouteTests(unittest.TestCase):
    def test_counts_thread_networks_reached_through_a_router(self):
        # Same /64 via two routers counts once; on-link and non-/64 routes do not.
        self.assertEqual(m.thread_routes(ROUTES), 1)
        self.assertEqual(m.thread_routes(""), 0)

    def test_unreadable_route_table_is_unknown(self):
        with patch.object(m, "IPV6_ROUTE_FILE", Path("/nonexistent/ipv6_route")):
            self.assertIsNone(m.read_thread_routes())


class ListingTests(unittest.TestCase):
    def run_listing(self, rows, *, entries=None, flows=None, native=None, bridges=()):
        hass = SimpleNamespace()
        thread = {"routers": 1, "preferredNetwork": False, "routes": 0}

        async def resolver(_hass):
            return lambda model: (native or {}).get(model)

        async def passthrough(items):
            return items

        with (
            patch.object(m, "zeroconf_rows", AsyncMock(return_value=("zc", rows))),
            patch.object(m, "bluetooth_state", return_value=(1, [])),
            patch.object(m, "reachable", passthrough),
            patch.object(m, "own_bridge_ids", return_value=set(bridges)),
            patch.object(m, "configured_accessories", return_value=entries or {}),
            patch.object(m, "pending_flows", return_value=flows or {}),
            patch.object(m, "native_resolver", resolver),
            patch.object(m, "thread_state", AsyncMock(return_value=thread)),
        ):
            return asyncio.run(m.homekit_accessories(hass))

    def test_order_dedupe_flow_ids_and_readiness(self):
        result = self.run_listing(
            [
                row(id="aa:aa:aa:aa:aa:01", name="Zeta", paired=True),
                row(id="aa:aa:aa:aa:aa:02", name="Alpha", transport="ble"),
                row(id="aa:aa:aa:aa:aa:02", name="Alpha", transport="thread"),
                row(id="aa:aa:aa:aa:aa:03", name="Hue", model="BSB002", paired=True),
                row(id="aa:aa:aa:aa:aa:04", name="Own bridge"),
                row(id="aa:aa:aa:aa:aa:05", name="Beta", paired=True),
            ],
            entries={"aa:aa:aa:aa:aa:05": "entry-5"},
            flows={"aa:aa:aa:aa:aa:02": "flow-2", "aa:aa:aa:aa:aa:01": "flow-x"},
            native={"BSB002": "hue"},
            bridges={"aa:aa:aa:aa:aa:04"},
        )
        listed = [
            (item["name"], item["status"], item["transport"], item["flowId"])
            for item in result["accessories"]
        ]
        self.assertEqual(
            listed,
            [
                ("Alpha", "ready", "thread", "flow-2"),
                ("Zeta", "paired_elsewhere", "ip", ""),
                ("Hue", "native", "ip", ""),
                ("Beta", "added", "ip", ""),
            ],
        )
        self.assertEqual(result["accessories"][2]["nativeDomain"], "hue")
        self.assertEqual(result["accessories"][3]["entryId"], "entry-5")
        self.assertEqual(result["bluetooth"], {"adapters": 1})
        self.assertEqual(result["thread"]["routers"], 1)
        self.assertTrue(result["scannedAt"])


class HelperTests(unittest.TestCase):
    def test_configured_and_pending_by_accessory_id(self):
        entries = [
            SimpleNamespace(
                entry_id="e1",
                data={"AccessoryPairingID": "1A:2B:3C:4D:5E:6F"},
                unique_id=None,
            ),
            SimpleNamespace(entry_id="e2", data={}, unique_id="aa:bb:cc:dd:ee:ff"),
        ]
        flows = [
            {"flow_id": "f1", "context": {"unique_id": "aa:bb:cc:dd:ee:ff"}},
            {"flow_id": "f2", "context": {}},
        ]
        hass = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_entries=lambda domain: (
                    entries if domain == "homekit_controller" else []
                ),
                flow=SimpleNamespace(async_progress_by_handler=lambda handler: flows),
            )
        )
        self.assertEqual(
            m.configured_accessories(hass),
            {"1a:2b:3c:4d:5e:6f": "e1", "aa:bb:cc:dd:ee:ff": "e2"},
        )
        self.assertEqual(m.pending_flows(hass), {"aa:bb:cc:dd:ee:ff": "f1"})

    def test_reachable_keeps_answering_wifi_accessories_and_all_others(self):
        rows = [
            row(id="aa:aa:aa:aa:aa:01", host="192.168.68.80"),
            row(id="aa:aa:aa:aa:aa:02", host="192.168.68.81"),
            row(id="aa:aa:aa:aa:aa:03", host="", port=0),
            row(id="aa:aa:aa:aa:aa:04", transport="thread", host="fd11::5"),
            row(id="aa:aa:aa:aa:aa:05", transport="ble", host="", port=0),
        ]

        async def online(host, port):
            return host == "192.168.68.80"

        kept = asyncio.run(m.reachable(rows, probe=online))
        self.assertEqual([item["id"][-2:] for item in kept], ["01", "04", "05"])


def matter_service(instance="8F3B2E1A2D4C6E7F", **properties):
    props = {
        b"D": b"3840",
        b"VP": b"4107+9",
        b"CM": b"1",
        b"DT": b"269",
        b"DN": b"WiZ Downlight",
    }
    props |= {key.encode(): value for key, value in properties.items()}
    return SimpleNamespace(
        name=f"{instance}._matterc._udp.local.",
        properties={key: value for key, value in props.items() if value is not None},
        port=5540,
        parsed_addresses=lambda: ["fe80::2", "192.168.68.85"],
    )


def matter_advert(discriminator=3840, vendor=4107, product=9, opcode=0x00):
    return (
        bytes([opcode])
        + (discriminator & 0x0FFF).to_bytes(2, "little")
        + vendor.to_bytes(2, "little")
        + product.to_bytes(2, "little")
        + b"\x00"
    )


class MatterTests(unittest.TestCase):
    def test_network_device_in_its_pairing_window(self):
        row = m.matter_service_row(matter_service(), 1763560800000)
        self.assertEqual(
            {k: row[k] for k in ("id", "name", "vendorId", "productId", "deviceType")},
            {
                "id": "8f3b2e1a2d4c6e7f",
                "name": "WiZ Downlight",
                "vendorId": 4107,
                "productId": 9,
                "deviceType": 269,
            },
        )
        self.assertEqual((row["commissioningMode"], row["discriminator"]), (1, 3840))
        self.assertEqual(
            (row["transport"], row["host"], row["port"]), ("ip", "192.168.68.85", 5540)
        )
        self.assertTrue(row["lastSeen"].startswith("2025-11-19T"))
        shared = m.matter_service_row(matter_service(CM=b"2", DN=None, VP=b"4874"))
        self.assertEqual(
            (shared["commissioningMode"], shared["name"], shared["productId"]),
            (2, "", 0),
        )

    def test_closed_windows_and_bad_instances_are_skipped(self):
        self.assertIsNone(m.matter_service_row(matter_service(CM=b"0")))
        self.assertIsNone(m.matter_service_row(matter_service(CM=None)))
        self.assertIsNone(m.matter_service_row(matter_service(instance="not hex!")))
        self.assertEqual(
            m.matter_service_row(matter_service(D=b"9999"))["discriminator"], -1
        )

    def test_bluetooth_advertisement_layout_and_freshness(self):
        info = SimpleNamespace(
            service_data={
                m.MATTER_BLE_UUID: matter_advert(0xF00 | 0x0A, 0x1349, 0x0101)
            },
            address="AA:BB:CC:DD:EE:01",
            name="",
            time=990.0,
        )
        row = m.matter_ble_row(info, 1000.0, 1763560800.0)
        self.assertEqual(
            (row["id"], row["transport"], row["discriminator"]),
            ("aa:bb:cc:dd:ee:01", "ble", 0xF0A),
        )
        self.assertEqual((row["vendorId"], row["productId"]), (0x1349, 0x0101))
        self.assertIsNone(
            m.matter_ble_row(
                SimpleNamespace(**{**info.__dict__, "time": 600.0}), 1000.0, 0
            )
        )
        self.assertIsNone(
            m.matter_ble_row(
                SimpleNamespace(
                    **{
                        **info.__dict__,
                        "service_data": {m.MATTER_BLE_UUID: matter_advert(opcode=1)},
                    }
                ),
                1000.0,
                0,
            )
        )
        self.assertIsNone(
            m.matter_ble_row(
                SimpleNamespace(**{**info.__dict__, "service_data": {}}), 1000.0, 0
            )
        )
        self.assertIsNone(
            m.matter_ble_row(
                SimpleNamespace(**{**info.__dict__, "address": "uuid-like"}), 1000.0, 0
            )
        )

    def test_one_row_per_device_network_first(self):
        ble_same = SimpleNamespace(
            service_data={m.MATTER_BLE_UUID: matter_advert(3840, 4107, 9)},
            address="AA:BB:CC:DD:EE:02",
            name="WiZ",
            time=999.0,
        )
        ble_other = SimpleNamespace(
            service_data={m.MATTER_BLE_UUID: matter_advert(1234, 4937, 42)},
            address="AA:BB:CC:DD:EE:03",
            name="",
            time=999.0,
        )
        devices = m.matter_devices(
            [(matter_service(), 0)], [ble_same, ble_other], 1000.0, 1763560800.0
        )
        self.assertEqual(
            [(d["transport"], d["id"]) for d in devices],
            [("ip", "8f3b2e1a2d4c6e7f"), ("ble", "aa:bb:cc:dd:ee:03")],
        )

    def test_matter_listing_failure_leaves_homekit_rows(self):
        self.assertEqual(m.pairable_matter("not-a-zeroconf", [], 0.0, 0.0), [])


if __name__ == "__main__":
    unittest.main()
