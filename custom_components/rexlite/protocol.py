"""Versioned REXLiTE tunnel protocol helpers.

This module intentionally has no Home Assistant imports so the wire contract can
be tested without booting Home Assistant.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

TYPE_HELLO: Final = "hello"
TYPE_HEARTBEAT: Final = "heartbeat"
TYPE_HEARTBEAT_ACK: Final = "heartbeat_ack"
TYPE_PROXY_REQUEST: Final = "proxy_request"
TYPE_PROXY_RESPONSE: Final = "proxy_response"
TYPE_STREAM_OPEN: Final = "stream_open"
TYPE_STREAM_ACCEPT: Final = "stream_accept"
TYPE_STREAM_DATA: Final = "stream_data"
TYPE_STREAM_CLOSE: Final = "stream_close"
TYPE_OTA_MANIFEST: Final = "ota_manifest"
TYPE_ERROR: Final = "error"

_VALID_GATEWAY_SCHEMES: Final = frozenset({"ws", "wss"})
_VALID_LOCAL_SCHEMES: Final = frozenset({"http", "https"})


class ProtocolError(ValueError):
    """Raised when a tunnel message is malformed."""


@dataclass(frozen=True, slots=True)
class Envelope:
    """Validated tunnel envelope."""

    message_type: str
    request_id: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class HttpResponseBodyPlan:
    """Describe how a raw HTTP/1.1 response body must be consumed."""

    mode: str
    length: int = 0


def http_response_body_plan(
    method: str,
    status: int,
    headers: Mapping[str, Sequence[str]],
) -> HttpResponseBodyPlan:
    """Return RFC-compatible framing for a proxied HTTP response body."""

    normalized: dict[str, list[str]] = {}
    for key, values in headers.items():
        normalized.setdefault(key.lower(), []).extend(values)

    if status == 101:
        return HttpResponseBodyPlan("close")
    if method.upper() == "HEAD" or 100 <= status < 200 or status in (204, 304):
        return HttpResponseBodyPlan("none")

    transfer_encoding = ",".join(normalized.get("transfer-encoding", ()))
    if transfer_encoding:
        encodings = [item.strip().lower() for item in transfer_encoding.split(",")]
        if encodings != ["chunked"]:
            raise ProtocolError("unsupported Home Assistant transfer encoding")
        return HttpResponseBodyPlan("chunked")

    content_lengths = {
        item.strip()
        for value in normalized.get("content-length", ())
        for item in value.split(",")
        if item.strip()
    }
    if content_lengths:
        if len(content_lengths) != 1:
            raise ProtocolError("conflicting Home Assistant content lengths")
        try:
            length = int(content_lengths.pop())
        except ValueError as err:
            raise ProtocolError("invalid Home Assistant content length") from err
        if length < 0:
            raise ProtocolError("invalid Home Assistant content length")
        return HttpResponseBodyPlan("fixed", length)

    return HttpResponseBodyPlan("close")


def utc_timestamp() -> str:
    """Return an RFC 3339 UTC timestamp compatible with the Go gateway."""

    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def make_envelope(
    message_type: str,
    request_id: str = "",
    payload: dict[str, Any] | None = None,
) -> str:
    """Serialize a tunnel envelope with compact, deterministic JSON."""

    if not message_type:
        raise ProtocolError("message type is required")
    envelope: dict[str, Any] = {
        "type": message_type,
        "timestamp": utc_timestamp(),
    }
    if request_id:
        envelope["request_id"] = request_id
    if payload is not None:
        envelope["payload"] = payload
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))


def parse_envelope(raw: str | bytes) -> Envelope:
    """Parse and validate a tunnel envelope."""

    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        raise ProtocolError("message is not valid JSON") from err
    if not isinstance(value, dict):
        raise ProtocolError("message must be a JSON object")

    message_type = value.get("type")
    request_id = value.get("request_id", "")
    payload = value.get("payload", {})
    if not isinstance(message_type, str) or not message_type:
        raise ProtocolError("message type is required")
    if not isinstance(request_id, str):
        raise ProtocolError("request_id must be a string")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ProtocolError("payload must be an object")
    return Envelope(message_type, request_id, payload)


def encode_bytes(value: bytes) -> str:
    """Encode bytes using Go encoding/json's []byte representation."""

    return base64.b64encode(value).decode("ascii")


def decode_bytes(value: Any, *, maximum: int) -> bytes:
    """Decode and size-limit a Go-compatible JSON byte string."""

    if value in (None, ""):
        return b""
    if not isinstance(value, str):
        raise ProtocolError("binary payload must be a base64 string")
    # Reject obviously oversized input before allocating the decoded buffer.
    if len(value) > ((maximum + 2) // 3) * 4 + 4:
        raise ProtocolError(f"binary payload exceeds {maximum} bytes")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as err:
        raise ProtocolError("binary payload is not valid base64") from err
    if len(decoded) > maximum:
        raise ProtocolError(f"binary payload exceeds {maximum} bytes")
    return decoded


def validate_gateway_url(value: str) -> str:
    """Validate and normalize a gateway WebSocket URL."""

    parsed = urlsplit(value.strip())
    if parsed.scheme.lower() not in _VALID_GATEWAY_SCHEMES or not parsed.hostname:
        raise ProtocolError("gateway URL must use ws:// or wss:// and include a host")
    if parsed.username or parsed.password or parsed.fragment:
        raise ProtocolError("gateway URL must not include credentials or a fragment")
    try:
        _ = parsed.port
    except ValueError as err:
        raise ProtocolError("gateway URL contains an invalid port") from err
    path = parsed.path or "/ws/agent"
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, path, parsed.query, ""))


def validate_local_url(value: str) -> str:
    """Validate and normalize the local Home Assistant URL."""

    parsed = urlsplit(value.strip())
    if parsed.scheme.lower() not in _VALID_LOCAL_SCHEMES or not parsed.hostname:
        raise ProtocolError(
            "Home Assistant URL must use http:// or https:// and include a host"
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ProtocolError(
            "Home Assistant URL must not include credentials, a query, or a fragment"
        )
    try:
        _ = parsed.port
    except ValueError as err:
        raise ProtocolError("Home Assistant URL contains an invalid port") from err
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, path, "", ""))


def agent_websocket_url(
    gateway_url: str,
    *,
    agent_id: str,
    version: str,
    remote_admin_enabled: bool,
) -> str:
    """Build the authenticated agent connection URL."""

    parsed = urlsplit(validate_gateway_url(gateway_url))
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(
        {
            "agent_id": agent_id,
            "version": version,
            "meta_role": "home_assistant",
            "meta_remote_admin_enabled": str(remote_admin_enabled).lower(),
            "meta_remote_access_mode": (
                "full_control" if remote_admin_enabled else "health_only"
            ),
            "meta_remote_health_monitoring_enabled": "true",
        }
    )
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def gateway_http_url(gateway_url: str, path: str) -> str:
    """Convert a gateway WebSocket URL to its HTTPS API URL."""

    parsed = urlsplit(validate_gateway_url(gateway_url))
    scheme = "https" if parsed.scheme == "wss" else "http"
    safe_path = "/" + path.lstrip("/")
    return urlunsplit((scheme, parsed.netloc, safe_path, "", ""))


def credential_probe_url(gateway_url: str, agent_id: str) -> str:
    """Return the non-mutating API endpoint used to validate enrollment auth."""

    return gateway_http_url(
        gateway_url,
        f"/api/v1/agents/{quote(agent_id, safe='')}/home-assistant-token",
    )


def local_request_url(base_url: str, path: str, raw_query: str = "") -> str:
    """Join an untrusted request path to the configured local HA origin."""

    if not isinstance(path, str) or not path.startswith("/"):
        raise ProtocolError("request path must start with /")
    if "\r" in path or "\n" in path or "\r" in raw_query or "\n" in raw_query:
        raise ProtocolError("request target contains invalid characters")
    parsed = urlsplit(validate_local_url(base_url))
    base_path = parsed.path.rstrip("/")
    joined_path = f"{base_path}{path}" or "/"
    return urlunsplit((parsed.scheme, parsed.netloc, joined_path, raw_query, ""))
