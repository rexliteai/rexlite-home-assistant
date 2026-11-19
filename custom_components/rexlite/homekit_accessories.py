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
"""

from __future__ import annotations

import asyncio
import time
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
RTF_GATEWAY = 0x0002
PROBE_CONCURRENCY = 16
PROBE_DEADLINE = 8
# DNS record type and class, as HA's Thread discovery reads its cache.
TYPE_PTR = 12
CLASS_IN = 1


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


async def zeroconf_rows(hass):
    """HA's zeroconf instance and every cached ``_hap`` service on it."""
    from homeassistant.components import zeroconf
    from zeroconf import BadTypeInNameException
    from zeroconf.asyncio import AsyncServiceInfo

    aiozc = await zeroconf.async_get_async_instance(hass)
    rows: list[dict] = []
    for service_type, transport in HAP_TYPES:
        records = aiozc.zeroconf.cache.async_all_by_details(
            service_type, TYPE_PTR, CLASS_IN
        )
        for record in records:
            try:
                info = AsyncServiceInfo(service_type, record.alias)
            except BadTypeInNameException:
                continue
            if not info.load_from_cache(aiozc.zeroconf):
                continue  # Not fully cached yet: HA's browser fills it shortly.
            if row := service_row(info, transport, getattr(record, "created", 0)):
                rows.append(row)
    return aiozc, rows


def bluetooth_state(hass) -> tuple[int, list]:
    """Connectable adapters and current advertisements (none without Bluetooth)."""
    try:
        from homeassistant.components import bluetooth

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
        from homeassistant.components.zeroconf.discovery import (
            async_get_homekit_discovery,
            build_homekit_model_lookups,
        )
        from homeassistant.loader import async_get_homekit

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
        from homeassistant.components.thread.discovery import (
            async_read_zeroconf_cache,
        )

        routers = len(
            {row.extended_address for row in async_read_zeroconf_cache(aiozc)}
        )
    except Exception:  # noqa: BLE001 - reported as no routers
        routers = 0
    preferred = False
    try:
        from homeassistant.components.thread import async_get_preferred_dataset

        preferred = bool(await async_get_preferred_dataset(hass))
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
