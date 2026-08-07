"""Safe local IPv4 discovery for the REXLiTE Home Assistant host."""

from __future__ import annotations

import ipaddress
import os
import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

_PRIVATE_NETWORKS: Final = tuple(
    ipaddress.ip_network(cidr)
    for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
_CONTAINER_BRIDGE_RANGE: Final = ipaddress.ip_network("172.16.0.0/12")
_DOCKER_BRIDGE_GATEWAY: Final = ipaddress.ip_address("172.17.0.1")


@dataclass(frozen=True, slots=True)
class IPCLanAddress:
    """One validated LAN address and the method that found it."""

    address: str
    source: str


def detect_ipc_lan_ipv4(
    home_assistant_url: str,
    *,
    route_probe: Callable[[], str | None] | None = None,
    hostname_probe: Callable[[], Iterable[str]] | None = None,
    assigned_probe: Callable[[str], bool] | None = None,
    containerized: bool | None = None,
) -> IPCLanAddress | None:
    """Return the best safe RFC1918 IPv4 address for this HA installation."""

    is_containerized = (
        _running_in_container() if containerized is None else containerized
    )
    route_candidate = (route_probe or _probe_default_route)()
    route_address = _valid_private_ipv4(
        route_candidate,
        reject_docker_bridge=is_containerized,
    )
    if route_address is not None:
        return IPCLanAddress(str(route_address), "default_route")

    for candidate in (hostname_probe or _hostname_candidates)():
        hostname_address = _valid_private_ipv4(
            candidate,
            reject_docker_bridge=is_containerized,
        )
        if hostname_address is not None:
            return IPCLanAddress(str(hostname_address), "hostname")

    configured_host = urlsplit(home_assistant_url).hostname or ""
    configured = _valid_private_ipv4(
        configured_host,
        reject_docker_bridge=is_containerized,
    )
    is_assigned = assigned_probe or _is_assigned_local_address
    if configured is not None and is_assigned(str(configured)):
        return IPCLanAddress(str(configured), "home_assistant_url")

    return None


def _valid_private_ipv4(
    value: object,
    *,
    reject_docker_bridge: bool = False,
) -> ipaddress.IPv4Address | None:
    candidate = str(value or "").strip()
    if not candidate:
        return None

    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return None

    if not isinstance(address, ipaddress.IPv4Address):
        return None
    if address == _DOCKER_BRIDGE_GATEWAY:
        return None
    if not any(address in network for network in _PRIVATE_NETWORKS):
        return None
    if reject_docker_bridge and address in _CONTAINER_BRIDGE_RANGE:
        return None
    return address


def _probe_default_route() -> str | None:
    """Read the kernel-selected source address without sending application data."""

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.settimeout(0.2)
            probe.connect(("1.1.1.1", 443))
            return str(probe.getsockname()[0])
    except OSError:
        return None


def _hostname_candidates() -> Iterable[str]:
    try:
        results = socket.getaddrinfo(
            socket.gethostname(),
            None,
            family=socket.AF_INET,
            type=socket.SOCK_DGRAM,
        )
    except OSError:
        return ()
    return tuple(str(result[4][0]) for result in results)


def _is_assigned_local_address(address: str) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((address, 0))
        return True
    except OSError:
        return False


def _running_in_container() -> bool:
    return os.path.exists("/.dockerenv") or bool(
        os.environ.get("CONTAINER", "").strip()
    )
