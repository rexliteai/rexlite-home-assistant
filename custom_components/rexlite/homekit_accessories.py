"""HomeKit accessories the hub sees, for the MAX'Is HomeKit tile.

Pairing stays with Home Assistant's own HomeKit Device integration
(``homekit_controller``). This read-only command lists what the hub sees, so the
platform can show every "Works with Apple Home" accessory, including the ones
another controller (Apple Home) already owns: HA aborts their discovery
silently, so they never appear among its config flows.

- Wi-Fi/Ethernet (``_hap._tcp``) and Thread (``_hap._udp``) accessories come
  from HA's zeroconf cache, which HA keeps browsing for homekit_controller. A
  Wi-Fi accessory must also answer a TCP handshake, so a switched-off device
  whose records are still cached is left out.
- Bluetooth accessories come from HA's advertisements carrying Apple's HAP data.

"Works with Apple Home" also covers Matter devices, which Apple Home lists as
"other Matter accessories". Those waiting to be paired are listed under
``matter``: ``_matterc._udp`` on the network (HA browses it for its Matter
integration) and Matter's Bluetooth advertisement for devices not on Wi-Fi yet.
The platform pairs them with its Matter wizard and the device's setup code.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import Counter
from datetime import UTC, datetime
from ipaddress import ip_address
from pathlib import Path
from typing import Any

DATA_KEY = "rexlite_homekit_accessories"
HAP_TYPES = (("_hap._tcp.local.", "ip"), ("_hap._udp.local.", "thread"))
TRANSPORT_ORDER = ("ip", "thread", "ble")
STATUS_ORDER = ("ready", "paired_elsewhere", "native", "added", "ignored")
APPLE_MANUFACTURER_ID = 76
HAP_BLE_ADVERTISEMENT = 0x06
# A Bluetooth advertisement older than this is a device that left or was switched off.
BLE_FRESH_SECONDS = 300
# Models HA's HomeKit Device integration refuses (homekit_controller HOMEKIT_IGNORE).
IGNORED_MODELS = frozenset({"T8400", "T8410", "HHKBridge1,1"})
IPV6_ROUTE_FILE = Path("/proc/net/ipv6_route")
IPV6_ADDRESS_FILE = Path("/proc/net/if_inet6")
IPV6_DISABLED_FILE = Path("/proc/sys/net/ipv6/conf/all/disable_ipv6")
# Services counted in the diagnostics; Apple TV and Cast show mDNS reaches HA at all.
DIAGNOSTIC_TYPES = (
    "_matterc._udp.local.",
    "_matter._tcp.local.",
    "_hap._tcp.local.",
    "_hap._udp.local.",
    "_meshcop._udp.local.",
    "_airplay._tcp.local.",
    "_googlecast._tcp.local.",
)
TYPE_A = 1
TYPE_AAAA = 28
IPV6_SCOPES = {0x00: "global", 0x10: "host", 0x20: "link", 0x40: "site"}
RTF_GATEWAY = 0x0002
PROBE_CONCURRENCY = 16
PROBE_DEADLINE = 8
# DNS record type and class, as HA's Thread discovery reads its cache.
TYPE_PTR = 12
CLASS_IN = 1
# Matter commissionable nodes: DNS-SD on the network, service data 0xFFF6 over BLE.
MATTER_TYPE = "_matterc._udp.local."
MATTER_BLE_UUID = "0000fff6-0000-1000-8000-00805f9b34fb"
MATTER_INSTANCE = re.compile(r"[0-9a-f]{8,32}")


def _component(name: str):
    # Not a static import: REXLiTE AI must load when this integration is
    # missing or failed its setup (tests/test_manifest_dependencies.py).
    return __import__(f"homeassistant.components.{name}", fromlist=("__name__",))


def text_value(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace").strip()
    return "" if value is None else str(value).strip()


def int_value(value: Any, default: int) -> int:
    try:
        return int(text_value(value))
    except ValueError:
        return default


def normalize_id(value: Any) -> str:
    """A HAP device id (``1A:2B:3C:4D:5E:6F``) in lower case, else ''."""
    text = text_value(value).lower()
    parts = text.split(":")
    if len(parts) == 6 and all(
        len(part) == 2 and all(char in "0123456789abcdef" for char in part)
        for part in parts
    ):
        return text
    return ""


def is_paired(status_flags: int) -> bool:
    """HAP status flag bit 0 stays set while no controller owns the accessory."""
    return not status_flags & 0x01


def lan_host(addresses: list[str]) -> str:
    """A private IPv4 first, else a unique-local IPv6 (Thread accessories).

    Public, loopback, link-local and multicast addresses are never used.
    """
    ipv4: list[str] = []
    ipv6: list[str] = []
    for value in addresses:
        try:
            address = ip_address(str(value).split("%", 1)[0])
        except ValueError:
            continue
        if (
            not address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_unspecified
        ):
            continue
        (ipv4 if address.version == 4 else ipv6).append(str(address))
    return (ipv4 or ipv6 or [""])[0]


def iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat()


def service_row(info, transport: str, seen_ms: float = 0) -> dict | None:
    """One cached ``_hap`` service as the platform lists it."""
    properties = {
        text_value(key).lower(): value
        for key, value in (getattr(info, "properties", None) or {}).items()
    }
    device_id = normalize_id(properties.get("id"))
    if not device_id:
        return None
    model = text_value(properties.get("md"))
    name = text_value(getattr(info, "name", ""))
    suffix = "." + text_value(getattr(info, "type", ""))
    if suffix != "." and name.lower().endswith(suffix.lower()):
        name = name[: -len(suffix)]
    try:
        addresses = list(info.parsed_addresses())
    except Exception:  # noqa: BLE001 - a malformed record only loses its address
        addresses = []
    port = getattr(info, "port", 0)
    return {
        "id": device_id,
        "name": name.strip() or model,
        "model": model,
        "category": int_value(properties.get("ci"), 1) or 1,
        "transport": transport,
        "host": lan_host(addresses),
        "port": port if isinstance(port, int) and 0 < port < 65536 else 0,
        "paired": is_paired(int_value(properties.get("sf"), 1)),
        "lastSeen": iso(seen_ms / 1000) if seen_ms else "",
    }


def ble_advertisement(manufacturer_data: dict) -> dict | None:
    """Apple's HAP advertisement: type 0x06, status flags, device id, category."""
    data = (manufacturer_data or {}).get(APPLE_MANUFACTURER_ID)
    if not data or len(data) < 15 or data[0] != HAP_BLE_ADVERTISEMENT:
        return None
    return {
        "id": ":".join(f"{byte:02x}" for byte in data[3:9]),
        "statusFlags": data[2],
        "category": int.from_bytes(data[9:11], "little") or 1,
    }


def ble_row(info, now_monotonic: float, now_wall: float) -> dict | None:
    advertisement = ble_advertisement(getattr(info, "manufacturer_data", None))
    if advertisement is None:
        return None
    seen = getattr(info, "time", None)
    age = now_monotonic - seen if isinstance(seen, (int, float)) else None
    if age is not None and age > BLE_FRESH_SECONDS:
        return None
    name = text_value(getattr(info, "name", ""))
    if name.lower() == text_value(getattr(info, "address", "")).lower():
        name = ""
    return {
        "id": advertisement["id"],
        "name": name,
        "model": "",
        "category": advertisement["category"],
        "transport": "ble",
        "host": "",
        "port": 0,
        "paired": is_paired(advertisement["statusFlags"]),
        "lastSeen": iso(now_wall - max(age, 0)) if age is not None else "",
    }


def accessory_status(
    row: dict, entries: dict[str, str], native_domain: str | None
) -> tuple[str, str, str]:
    """(status, nativeDomain, entryId) for one accessory.

    An HA entry whose accessory now reports unpaired was reset; HA removes that
    stale pairing and offers the accessory again, so it is ready to pair.
    """
    entry_id = entries.get(row["id"], "")
    if entry_id and row["paired"]:
        return "added", "", entry_id
    if row["model"] in IGNORED_MODELS:
        return "ignored", "", ""
    if native_domain:
        return "native", native_domain, ""
    if row["paired"]:
        return "paired_elsewhere", "", ""
    return "ready", "", ""


def thread_routes(text: str) -> int:
    """Thread networks the host can route to (``/proc/net/ipv6_route``).

    A border router announces its Thread network's /64 (a unique-local fd…
    prefix) in IPv6 router advertisements. The kernel installs that route via
    the router's link-local address only when the host accepts route
    information options, which Thread accessories need to be reachable.
    """
    destinations: set[str] = set()
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 10:
            continue
        destination, prefix_length, next_hop, flags = (
            fields[0],
            fields[1],
            fields[4],
            fields[8],
        )
        try:
            if (
                destination.startswith("fd")
                and int(prefix_length, 16) == 64
                and next_hop.startswith("fe80")
                and int(flags, 16) & RTF_GATEWAY
            ):
                destinations.add(destination)
        except ValueError:
            continue
    return len(destinations)


def read_thread_routes() -> int | None:
    try:
        return thread_routes(IPV6_ROUTE_FILE.read_text(encoding="ascii"))
    except (OSError, UnicodeDecodeError):
        return None


def cached_services(aiozc, service_type: str) -> list:
    """(service info, PTR record time) for each fully cached instance of a type."""
    from zeroconf import BadTypeInNameException
    from zeroconf.asyncio import AsyncServiceInfo

    found = []
    records = aiozc.zeroconf.cache.async_all_by_details(
        service_type, TYPE_PTR, CLASS_IN
    )
    for record in records:
        try:
            info = AsyncServiceInfo(service_type, record.alias)
        except BadTypeInNameException:
            continue
        # Not fully cached yet: HA's browser fills it shortly.
        if info.load_from_cache(aiozc.zeroconf):
            found.append((info, getattr(record, "created", 0)))
    return found


async def zeroconf_rows(hass):
    """HA's zeroconf instance and every cached ``_hap`` service on it."""
    zeroconf = _component("zeroconf")
    aiozc = await zeroconf.async_get_async_instance(hass)
    rows = [
        row
        for service_type, transport in HAP_TYPES
        for info, seen in cached_services(aiozc, service_type)
        if (row := service_row(info, transport, seen))
    ]
    return aiozc, rows


def matter_service_row(info, seen_ms: float = 0) -> dict | None:
    """A Matter device announcing on the network that it can be paired.

    It does so while its commissioning window is open: ``CM=1`` after power-on or
    a reset, ``CM=2`` when another controller (Apple Home, Google Home…) opened
    it to share the device.
    """
    properties = {
        text_value(key).upper(): value
        for key, value in (getattr(info, "properties", None) or {}).items()
    }
    mode = int_value(properties.get("CM"), 0)
    if mode not in (1, 2):
        return None
    instance = text_value(getattr(info, "name", "")).lower()
    suffix = "." + MATTER_TYPE
    if instance.endswith(suffix):
        instance = instance[: -len(suffix)]
    if not MATTER_INSTANCE.fullmatch(instance):
        return None
    vendor, _, product = text_value(properties.get("VP")).partition("+")
    discriminator = int_value(properties.get("D"), -1)
    try:
        addresses = list(info.parsed_addresses())
    except Exception:  # noqa: BLE001 - a malformed record only loses its address
        addresses = []
    port = getattr(info, "port", 0)
    return {
        "id": instance,
        "name": text_value(properties.get("DN")),
        "vendorId": int_value(vendor, 0),
        "productId": int_value(product, 0),
        "deviceType": int_value(properties.get("DT"), 0),
        "commissioningMode": mode,
        "discriminator": discriminator if 0 <= discriminator <= 0x0FFF else -1,
        "transport": "ip",
        "host": lan_host(addresses),
        "port": port if isinstance(port, int) and 0 < port < 65536 else 0,
        "lastSeen": iso(seen_ms / 1000) if seen_ms else "",
    }


def matter_ble_row(info, now_monotonic: float, now_wall: float) -> dict | None:
    """A Matter device offering pairing over Bluetooth: not on the network yet."""
    data = (getattr(info, "service_data", None) or {}).get(MATTER_BLE_UUID)
    # Opcode 0 = commissionable; then version/discriminator, vendor and product ids.
    if not data or len(data) < 7 or data[0] != 0x00:
        return None
    seen = getattr(info, "time", None)
    age = now_monotonic - seen if isinstance(seen, (int, float)) else None
    if age is not None and age > BLE_FRESH_SECONDS:
        return None
    address = normalize_id(getattr(info, "address", ""))
    if not address:
        return None
    name = text_value(getattr(info, "name", ""))
    if name.lower() == address:
        name = ""
    return {
        "id": address,
        "name": name,
        "vendorId": int.from_bytes(data[3:5], "little"),
        "productId": int.from_bytes(data[5:7], "little"),
        "deviceType": 0,
        "commissioningMode": 1,
        "discriminator": int.from_bytes(data[1:3], "little") & 0x0FFF,
        "transport": "ble",
        "host": "",
        "port": 0,
        "lastSeen": iso(now_wall - max(age, 0)) if age is not None else "",
    }


def matter_devices(
    services, advertisements, now_monotonic: float, now_wall: float
) -> list[dict]:
    """Pairable Matter devices, one row each (the network row when seen both ways)."""
    rows = [row for info, seen in services if (row := matter_service_row(info, seen))]
    rows.extend(
        row
        for info in advertisements
        if (row := matter_ble_row(info, now_monotonic, now_wall))
    )
    ids: set[str] = set()
    keys: set[tuple] = set()
    devices: list[dict] = []
    for row in sorted(rows, key=lambda row: row["transport"] != "ip"):
        key = (row["vendorId"], row["productId"], row["discriminator"])
        if row["id"] in ids or (row["discriminator"] >= 0 and key in keys):
            continue
        ids.add(row["id"])
        keys.add(key)
        devices.append(row)
    devices.sort(
        key=lambda row: (
            row["transport"] != "ip",
            row["name"].lower() or "~",
            row["id"],
        )
    )
    return devices


def ipv6_addresses(text: str) -> list[dict]:
    """The host's IPv6 addresses (``/proc/net/if_inet6``): interface and scope."""
    found = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 6 or len(fields[0]) != 32:
            continue
        try:
            address = ip_address(int(fields[0], 16).to_bytes(16, "big"))
            scope = int(fields[3], 16)
        except ValueError:
            continue
        found.append(
            {
                "interface": fields[5][:32],
                "scope": IPV6_SCOPES.get(scope, str(scope)),
                "address": str(address),
            }
        )
    return found[:24]


def read_host_ipv6() -> dict:
    """Whether the host runs IPv6, which Matter needs, and its IPv6 addresses."""
    try:
        disabled = IPV6_DISABLED_FILE.read_text(encoding="ascii").strip() == "1"
    except (OSError, UnicodeDecodeError):
        disabled = None
    try:
        addresses = ipv6_addresses(IPV6_ADDRESS_FILE.read_text(encoding="ascii"))
    except (OSError, UnicodeDecodeError):
        addresses = []
    return {"disabled": disabled, "addresses": addresses}


async def network_diagnostics(hass) -> dict:
    """HA's network adapters (what its zeroconf listens on) and the host's IPv6."""
    adapters = []
    try:
        network = _component("network")
        for adapter in await network.async_get_adapters(hass):
            adapters.append(
                {
                    "name": text_value(adapter.get("name"))[:32],
                    "enabled": bool(adapter.get("enabled")),
                    "default": bool(adapter.get("default")),
                    "ipv4": [
                        text_value(item.get("address"))
                        for item in adapter.get("ipv4") or []
                    ][:4],
                    "ipv6": [
                        text_value(item.get("address"))
                        for item in adapter.get("ipv6") or []
                    ][:6],
                }
            )
    except Exception:  # noqa: BLE001 - diagnostics never fail the listing
        adapters = []
    host = await hass.async_add_executor_job(read_host_ipv6)
    return {"adapters": adapters[:8], "hostIpv6": host}


def zeroconf_diagnostics(aiozc) -> dict:
    """What HA's zeroconf hears: its sockets, cached records and service types."""
    zeroconf = getattr(aiozc, "zeroconf", None)
    engine = getattr(zeroconf, "engine", None)
    sockets = [
        {
            "ipv6": bool(getattr(transport, "is_ipv6", False)),
            "address": text_value((getattr(transport, "sock_name", None) or ("",))[0])[
                :64
            ],
        }
        for transport in list(getattr(engine, "readers", None) or [])[:12]
    ]
    cache = getattr(zeroconf, "cache", None)
    records = [
        record
        for store in list(getattr(cache, "cache", {}).values())
        for record in list(store)
    ]
    kinds = Counter(getattr(record, "type", 0) for record in records)
    service_types = sorted(
        {
            text_value(getattr(record, "name", ""))
            for record in records
            if getattr(record, "type", 0) == TYPE_PTR
            and text_value(getattr(record, "name", "")).endswith(
                ("._tcp.local.", "._udp.local.")
            )
            and "._sub." not in text_value(getattr(record, "name", ""))
        }
    )
    counts = {}
    for service_type in DIAGNOSTIC_TYPES:
        try:
            pointers = cache.async_all_by_details(service_type, TYPE_PTR, CLASS_IN)
            counts[service_type] = len({record.alias for record in pointers})
        except Exception:  # noqa: BLE001 - an odd cache only loses its counts
            counts[service_type] = 0
    commissionable = []
    try:
        for info, _seen in cached_services(aiozc, MATTER_TYPE)[:10]:
            properties = {
                text_value(key).upper(): text_value(value)[:64]
                for key, value in (info.properties or {}).items()
            }
            commissionable.append(
                {
                    "instance": text_value(info.name)[:96],
                    "txt": {
                        key: properties[key]
                        for key in ("CM", "D", "VP", "DT", "DN", "PH")
                        if key in properties
                    },
                    "addresses": [str(item) for item in info.parsed_addresses()][:6],
                }
            )
    except Exception:  # noqa: BLE001 - diagnostics never fail the listing
        commissionable = []
    return {
        "sockets": sockets,
        "records": len(records),
        "addressRecords": {
            "ipv4": kinds.get(TYPE_A, 0),
            "ipv6": kinds.get(TYPE_AAAA, 0),
        },
        "serviceTypes": service_types[:60],
        "counts": counts,
        "commissionable": commissionable,
    }


def bluetooth_diagnostics(hass, advertisements, now_monotonic: float) -> dict:
    """Bluetooth scanners and what they currently hear (Matter and HomeKit counted)."""
    try:
        bluetooth = _component("bluetooth")
        scanners = bluetooth.async_scanner_count(hass, connectable=False)
        connectable = bluetooth.async_scanner_count(hass, connectable=True)
    except Exception:  # noqa: BLE001 - a hub without Bluetooth reports zero
        scanners = connectable = 0
    fresh = []
    for info in advertisements:
        seen = getattr(info, "time", None)
        if isinstance(seen, (int, float)) and now_monotonic - seen > BLE_FRESH_SECONDS:
            continue
        fresh.append(info)
    names = sorted(
        {
            text_value(getattr(info, "name", ""))[:40]
            for info in fresh
            if text_value(getattr(info, "name", ""))
            and text_value(getattr(info, "name", "")).lower()
            != text_value(getattr(info, "address", "")).lower()
        }
    )
    return {
        "scanners": scanners,
        "connectableScanners": connectable,
        "advertisements": len(advertisements),
        "fresh": len(fresh),
        "matter": sum(
            1
            for info in fresh
            if MATTER_BLE_UUID in (getattr(info, "service_data", None) or {})
        ),
        "homekit": sum(
            1
            for info in fresh
            if ble_advertisement(getattr(info, "manufacturer_data", None))
        ),
        "names": names[:20],
    }


async def homekit_diagnostics(hass, aiozc, advertisements, now_monotonic) -> dict:
    """Why a device may be missing: host IPv6, mDNS and Bluetooth as HA sees them."""
    try:
        network = await network_diagnostics(hass)
    except Exception:  # noqa: BLE001 - diagnostics never fail the listing
        network = {}
    try:
        zeroconf = zeroconf_diagnostics(aiozc)
    except Exception:  # noqa: BLE001
        zeroconf = {}
    try:
        bluetooth = bluetooth_diagnostics(hass, advertisements, now_monotonic)
    except Exception:  # noqa: BLE001
        bluetooth = {}
    return {"network": network, "zeroconf": zeroconf, "bluetooth": bluetooth}


def pairable_matter(aiozc, advertisements, now_monotonic, now_wall) -> list[dict]:
    try:
        services = cached_services(aiozc, MATTER_TYPE)
    except Exception:  # noqa: BLE001 - Matter rows are extra; HomeKit still lists
        services = []
    return matter_devices(services, advertisements, now_monotonic, now_wall)


def bluetooth_state(hass) -> tuple[int, list]:
    """Connectable adapters and current advertisements (none without Bluetooth)."""
    try:
        bluetooth = _component("bluetooth")
        adapters = bluetooth.async_scanner_count(hass, connectable=True)
        infos = list(bluetooth.async_discovered_service_info(hass, connectable=False))
    except Exception:  # noqa: BLE001 - a hub without Bluetooth still lists Wi-Fi
        return 0, []
    return adapters, infos


async def reachable(rows: list[dict], probe=None) -> list[dict]:
    """Drop Wi-Fi accessories that no longer answer on their HAP port."""
    if probe is None:
        from .device_discovery import endpoint_online as probe

    semaphore = asyncio.Semaphore(PROBE_CONCURRENCY)
    online: dict[int, bool] = {}

    async def check(index: int, row: dict) -> None:
        if not row["host"] or not row["port"]:
            return
        async with semaphore:
            online[index] = await probe(row["host"], row["port"])

    try:
        async with asyncio.timeout(PROBE_DEADLINE):
            async with asyncio.TaskGroup() as group:
                for index, row in enumerate(rows):
                    if row["transport"] == "ip":
                        group.create_task(check(index, row))
    except TimeoutError:
        pass  # Unfinished checks count as offline.
    return [
        row
        for index, row in enumerate(rows)
        if row["transport"] != "ip" or online.get(index)
    ]


def configured_accessories(hass) -> dict[str, str]:
    """HomeKit Device entries by accessory id."""
    found: dict[str, str] = {}
    for entry in hass.config_entries.async_entries("homekit_controller"):
        for value in (entry.data.get("AccessoryPairingID"), entry.unique_id):
            if device_id := normalize_id(value):
                found.setdefault(device_id, entry.entry_id)
    return found


def pending_flows(hass) -> dict[str, str]:
    """HomeKit Device flows in progress by accessory id."""
    flows: dict[str, str] = {}
    for flow in hass.config_entries.flow.async_progress_by_handler(
        "homekit_controller"
    ):
        device_id = normalize_id((flow.get("context") or {}).get("unique_id"))
        if device_id and flow.get("flow_id"):
            flows.setdefault(device_id, flow["flow_id"])
    return flows


def own_bridge_ids(hass) -> set[str]:
    """Accessories this HA exports itself (HomeKit Bridge), which are not devices."""
    from homeassistant.helpers import device_registry as dr

    bridges = {entry.entry_id for entry in hass.config_entries.async_entries("homekit")}
    if not bridges:
        return set()
    found: set[str] = set()
    for device in dr.async_get(hass).devices.values():
        if not bridges.intersection(device.config_entries):
            continue
        for kind, value in device.connections:
            if kind == dr.CONNECTION_NETWORK_MAC and (device_id := normalize_id(value)):
                found.add(device_id)
    return found


async def native_resolver(hass):
    """Model → integration HA uses instead of HomeKit Device for it, else None.

    Only models HA never offers to HomeKit Device count (``always_discover`` off,
    e.g. the Hue bridge): the others are offered to both.
    """
    try:
        from homeassistant.loader import async_get_homekit

        discovery = _component("zeroconf.discovery")
        async_get_homekit_discovery = discovery.async_get_homekit_discovery
        build_homekit_model_lookups = discovery.build_homekit_model_lookups

        lookups, matchers = build_homekit_model_lookups(await async_get_homekit(hass))
    except Exception:  # noqa: BLE001 - without the table every model is HomeKit
        return lambda model: None

    def resolve(model: str) -> str | None:
        if not model:
            return None
        found = async_get_homekit_discovery(lookups, matchers, {"md": model})
        return found.domain if found and not found.always_discover else None

    return resolve


async def thread_state(hass, aiozc) -> dict:
    """Border routers seen, HA's preferred Thread network, routes to Thread networks."""
    routers = 0
    try:
        async_read_zeroconf_cache = _component(
            "thread.discovery"
        ).async_read_zeroconf_cache
        routers = len(
            {row.extended_address for row in async_read_zeroconf_cache(aiozc)}
        )
    except Exception:  # noqa: BLE001 - reported as no routers
        routers = 0
    preferred = False
    try:
        thread = _component("thread")
        preferred = bool(await thread.async_get_preferred_dataset(hass))
    except Exception:  # noqa: BLE001 - reported as no preferred network
        preferred = False
    routes = await hass.async_add_executor_job(read_thread_routes)
    return {"routers": routers, "preferredNetwork": preferred, "routes": routes}


async def homekit_accessories(hass) -> dict:
    """Every HomeKit accessory the hub sees, with what the platform can do with it."""
    aiozc, rows = await zeroconf_rows(hass)
    adapters, advertisements = bluetooth_state(hass)
    now_monotonic, now_wall = time.monotonic(), time.time()
    rows.extend(
        row
        for info in advertisements
        if (row := ble_row(info, now_monotonic, now_wall))
    )
    rows = await reachable(rows)
    skipped = own_bridge_ids(hass)
    entries = configured_accessories(hass)
    flows = pending_flows(hass)
    resolve = await native_resolver(hass)
    seen: set[str] = set()
    accessories: list[dict] = []
    for row in sorted(rows, key=lambda row: TRANSPORT_ORDER.index(row["transport"])):
        if row["id"] in seen or row["id"] in skipped:
            continue
        seen.add(row["id"])
        status, native_domain, entry_id = accessory_status(
            row, entries, resolve(row["model"])
        )
        accessories.append(
            {
                **row,
                "status": status,
                "nativeDomain": native_domain,
                "flowId": flows.get(row["id"], "") if status == "ready" else "",
                "entryId": entry_id,
            }
        )
    accessories.sort(
        key=lambda row: (STATUS_ORDER.index(row["status"]), row["name"].lower())
    )
    return {
        "accessories": accessories,
        "matter": pairable_matter(aiozc, advertisements, now_monotonic, now_wall),
        "diagnostics": await homekit_diagnostics(
            hass, aiozc, advertisements, now_monotonic
        ),
        "bluetooth": {"adapters": adapters},
        "thread": await thread_state(hass, aiozc),
        "scannedAt": iso(now_wall),
    }


def register_homekit_accessories(hass) -> None:
    import voluptuous as vol
    from homeassistant.components import websocket_api

    if DATA_KEY in hass.data:
        return
    hass.data[DATA_KEY] = True

    @websocket_api.websocket_command(
        {vol.Required("type"): "rexlite/homekit/accessories"}
    )
    @websocket_api.require_admin
    @websocket_api.async_response
    async def list_homekit_accessories(hass, connection, msg):
        try:
            result = await homekit_accessories(hass)
        except Exception:  # noqa: BLE001 - discovery must not interrupt the tunnel
            connection.send_error(
                msg["id"],
                "homekit_scan_failed",
                "HomeKit 設備搜尋失敗，請確認主機探索服務",
            )
            return
        connection.send_result(msg["id"], result)

    websocket_api.async_register_command(hass, list_homekit_accessories)
