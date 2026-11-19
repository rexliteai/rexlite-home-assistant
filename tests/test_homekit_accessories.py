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


if __name__ == "__main__":
    unittest.main()
