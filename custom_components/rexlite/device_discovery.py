"""Bounded, administrator-only LAN discovery on the selected HA host."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from ipaddress import ip_address
from time import monotonic
from urllib.parse import urlsplit

DATA_KEY = "rexlite_device_discovery"


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

        cancel, browser = None, None
        try:
            async with asyncio.timeout(15):
                scanner = self.hass.data.get("ssdp", {}).get(SSDP_SCANNER)
                if scanner:
                    cancel = await ssdp.async_register_callback(self.hass, found)
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
            if cancel:
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
        flows = self.hass.config_entries.flow.async_progress()
        return {
            "flows": flows,
            "candidates": list(candidates.values()),
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
