"""Bounded, administrator-only LAN discovery on the selected HA host."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from ipaddress import ip_address
from time import monotonic
from urllib.parse import urlsplit

DATA_KEY = "rexlite_device_discovery"
ROUTER_SEARCH_TYPES = (
    "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
    "urn:schemas-upnp-org:device:InternetGatewayDevice:2",
)
CALLBACK_SETUP_TIMEOUT = 2


def local_origin(value: str) -> str:
    """Never offer credentials to public, malformed or credential-bearing URLs."""
    try:
        url = urlsplit(value)
        address = ip_address(url.hostname or "")
        if (
            url.scheme not in {"http", "https"}
            or url.username
            or url.password
            or not address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_unspecified
            or address.is_multicast
        ):
            return ""
        host = f"[{address}]" if address.version == 6 else str(address)
        return f"{url.scheme}://{host}" + (f":{url.port}" if url.port else "")
    except ValueError:
        return ""


def deco_candidate(info, configured_hosts: set[str]) -> dict | None:
    data = info.upnp
    maker = str(data.get("manufacturer", "")).lower().replace("-", "")
    model = str(data.get("modelName", ""))[:100]
    name = str(data.get("friendlyName", ""))[:160]
    if "tplink" not in maker or not (
        "deco" in f"{name} {model}".lower()
        or re.fullmatch(r"(?:X|XE|BE|M|E|S|P)\d{1,4}(?:[ -].*)?", model, re.I)
    ):
        return None
    host = local_origin(str(data.get("presentationURL", "")))
    host = host or local_origin(info.ssdp_location)
    if not host or urlsplit(host).hostname in configured_hosts:
        return None
    return {
        "id": f"tplink_deco:{host}",
        "handler": "tplink_deco",
        "name": f"TP-Link Deco {model}".strip(),
        "host": host,
        "source": "ssdp",
    }


def discovery_endpoint(info) -> tuple[str, int] | None:
    """Probe only the advertised LAN service, never guessed ports or subnets."""
    location = getattr(info, "ssdp_location", None)
    if location:
        origin = local_origin(location)
        if not origin:
            return None
        url = urlsplit(origin)
        return url.hostname, url.port or (443 if url.scheme == "https" else 80)
    if "._tcp." not in str(getattr(info, "type", "")):
        return None
    host, port = str(getattr(info, "host", "")), getattr(info, "port", None)
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        return None
    origin = local_origin(
        f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"
    )
    return (host, port) if origin else None


async def endpoint_online(host: str, port: int) -> bool:
    """A new TCP handshake; cached announcements are not liveness evidence."""
    writer = None
    try:
        async with asyncio.timeout(1.5):
            _, writer = await asyncio.open_connection(host, port)
            return True
    except (TimeoutError, OSError):
        return False
    finally:
        if writer:
            writer.close()
            try:
                async with asyncio.timeout(0.2):
                    await writer.wait_closed()
            except (TimeoutError, OSError):
                pass


async def online_discovery_flows(hass) -> list[dict]:
    from homeassistant.helpers.service_info.ssdp import SsdpServiceInfo
    from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

    manager = hass.config_entries.flow
    targets = {}
    for info_type in (SsdpServiceInfo, ZeroconfServiceInfo):
        observations = []
        manager.async_progress_by_init_data_type(
            info_type,
            lambda info, observations=observations: observations.append(info) is None,
        )
        for info in observations:
            endpoint = discovery_endpoint(info)
            if endpoint is None or (endpoint not in targets and len(targets) >= 128):
                continue
            matches = manager.async_progress_by_init_data_type(
                info_type, lambda other, info=info: other is info
            )
            targets.setdefault(endpoint, {}).update(
                (flow["flow_id"], flow)
                for flow in matches
                if flow.get("context", {}).get("source") in {"ssdp", "zeroconf"}
            )
    semaphore = asyncio.Semaphore(16)
    verified = {}

    async def check(endpoint, flows):
        async with semaphore:
            if await endpoint_online(*endpoint):
                for flow_id, flow in flows.items():
                    verified[flow_id] = {
                        **flow,
                        "context": {
                            **flow.get("context", {}),
                            "rexlite_online": True,
                            "rexlite_seen_at": datetime.now(UTC).isoformat(),
                        },
                    }

    try:
        async with asyncio.timeout(6):
            async with asyncio.TaskGroup() as group:
                for endpoint, flows in targets.items():
                    group.create_task(check(endpoint, flows))
    except TimeoutError:
        pass  # Keep only completed positive checks; never resurrect cached flows.
    active = {flow["flow_id"] for flow in manager.async_progress()}
    return [flow for key, flow in verified.items() if key in active]


class DeviceDiscovery:
    def __init__(self, hass):
        self.hass = hass
        self.lock = asyncio.Lock()
        self.result = None
        self.finished = 0.0

    async def scan(self):
        async with self.lock:
            if self.result is not None and monotonic() - self.finished < 15:
                return self.result
            result = await self._scan()
            self.result, self.finished = result, monotonic()
            return result

    async def _scan(self):
        from homeassistant.components import ssdp, zeroconf
        from homeassistant.components.ssdp.const import SSDP_SCANNER
        from homeassistant.loader import (
            IntegrationNotFound,
            async_get_integration,
            async_get_zeroconf,
        )
        from zeroconf.asyncio import AsyncServiceBrowser

        candidates, warnings = {}, []
        configured = {
            urlsplit(str(entry.data.get("host", ""))).hostname
            for entry in self.hass.config_entries.async_entries("tplink_deco")
        }
        receiving = False

        async def found(info, change):
            # Registration replays cached devices; only fresh network replies count.
            if receiving and change != ssdp.SsdpChange.BYEBYE:
                candidate = deco_candidate(info, configured)
                if candidate:
                    candidates[candidate["id"]] = candidate

        cancels, browser = [], None
        try:
            async with asyncio.timeout(15):
                scanner = self.hass.data.get("ssdp", {}).get(SSDP_SCANNER)
                if scanner:
                    # The HA callback API replays cached device descriptions before
                    # registering. A broad match waits on unrelated/offline devices.
                    # Limit Deco identification to gateway advertisements and never
                    # let replay prevent native SSDP and mDNS probes from running.
                    for search_type in ROUTER_SEARCH_TYPES:
                        try:
                            async with asyncio.timeout(CALLBACK_SETUP_TIMEOUT):
                                cancels.append(
                                    await ssdp.async_register_callback(
                                        self.hass, found, {"ST": search_type}
                                    )
                                )
                        except (TimeoutError, OSError):
                            if "部分路由器辨識逾時" not in warnings:
                                warnings.append("部分路由器辨識逾時")
                    receiving = True
                    await scanner.async_scan()
                else:
                    warnings.append("SSDP 探索尚未啟用")
                try:
                    types = list(await async_get_zeroconf(self.hass))
                    if types:
                        instance = await zeroconf.async_get_async_instance(self.hass)
                        browser = AsyncServiceBrowser(
                            instance.zeroconf, types, handlers=[lambda **kwargs: None]
                        )
                except (ImportError, KeyError, RuntimeError, OSError):
                    warnings.append("mDNS 探索暫時不可用")
                await asyncio.sleep(5)
        except (TimeoutError, OSError):
            warnings.append("部分探索逾時，請稍後重試")
        finally:
            receiving = False
            for cancel in cancels:
                cancel()
            if browser:
                await browser.async_cancel()
        if candidates:
            try:
                await async_get_integration(self.hass, "tplink_deco")
                installed = True
            except IntegrationNotFound:
                installed = False
            for candidate in candidates.values():
                candidate["installed"] = installed
        flows = await online_discovery_flows(self.hass)
        return {
            "flows": flows,
            "candidates": [
                {**c, "online": True, "seenAt": datetime.now(UTC).isoformat()}
                for c in candidates.values()
            ],
            "warnings": warnings,
            "scannedAt": datetime.now(UTC).isoformat(),
        }


def register_device_discovery(hass):
    import voluptuous as vol
    from homeassistant.components import websocket_api

    if DATA_KEY in hass.data:
        return
    scanner = hass.data[DATA_KEY] = DeviceDiscovery(hass)

    @websocket_api.websocket_command({vol.Required("type"): "rexlite/discovery/scan"})
    @websocket_api.require_admin
    @websocket_api.async_response
    async def scan(hass, connection, msg):
        try:
            result = await scanner.scan()
        except Exception:  # noqa: BLE001 - discovery must not interrupt the tunnel
            connection.send_error(
                msg["id"], "discovery_failed", "設備搜尋失敗，請確認主機探索服務"
            )
            return
        connection.send_result(msg["id"], result)

    websocket_api.async_register_command(hass, scan)
