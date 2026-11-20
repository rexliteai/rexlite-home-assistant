"""Tapo H110 infrared remotes for the MAX'Is operation platform (rexlite/tapo_ir).

The platform adds remotes, learns buttons from the original remote, presses
buttons and drives Tapo code-library air conditioners, without the Tapo App.

TP-Link does not publish the H110's IR methods. The requests below follow
Loadst0ne/tapo-ir-hub (MIT), which users run against real H110 hubs:

- hub: ``get_device_info``, ``get_child_device_list`` (remotes have category
  ``ir.remote``), ``addIrRemoteDevice``, ``startIrReceiveMode`` /
  ``getIrReceiveStatus`` / ``stopIrReceiveMode``
- remote, wrapped in ``control_child``: ``get_device_info``, ``setKeyInfo``,
  ``setDeviceInfo``, ``deleteRemote``, ``sendIrCmdById``, ``sendIrCmdByStatus``

Every change is read back from the H110 before it is reported as done, and a
half-created remote is deleted again.

The transport is python-kasa's KLAP protocol, which Home Assistant ships for its
TP-Link integration. This module adds no requirement: REXLiTE AI, and the tunnel
it carries, must load even where python-kasa is missing (``supported: false``).

Linking first tries without an account: python-kasa checks the H110's KLAP
challenge against blank credentials and TP-Link's setup credentials on this side,
so an H110 that never joined a Tapo account links with no login (and a bound one
sees no failed login). Only when that fails is the Tapo account (TP-Link ID) used,
once. The hub keeps only python-kasa's credentials hash in
``.storage/rexlite_tapo_ir`` (private) and a masked account hint for the
platform; the password is never stored or logged.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import logging
import re
import secrets
import time
import unicodedata
from copy import deepcopy
from datetime import UTC, datetime
from ipaddress import IPv4Address
from types import SimpleNamespace
from typing import Any

_LOGGER = logging.getLogger(__name__)

DATA_KEY = "rexlite_tapo_ir"
STORE_KEY = "rexlite_tapo_ir"
STORE_VERSION = 1
IR_CATEGORY = "ir.remote"
HUB_FAMILY = "SMART.TAPOHUB"
# Tapo hubs with an IR blaster; the H100 is the same family without one.
IR_HUB_MODELS = ("H110",)
DEFAULT_CONNECTION: dict[str, Any] = {
    "device_family": HUB_FAMILY,
    "encryption_type": "KLAP",
    "login_version": 2,
    "https": False,
    "http_port": 80,
}
REQUEST_TIMEOUT = 10
DISCOVERY_TIMEOUT = 3
LEARN_TIMEOUT = 30
LEARN_MIN, LEARN_MAX = 5, 120
LEARN_POLL_SECONDS = 0.5
STOP_TIMEOUT = 10
CAPTURE_TTL = 600
CAPTURE_LIMIT = 20
NAME_MAX = 64
LABEL_MAX = 32
ACCOUNT_MAX = 128

ACTIONS = (
    "hubs",
    "link",
    "unlink",
    "remotes",
    "learn",
    "learn_status",
    "learn_stop",
    "save_key",
    "delete_key",
    "rename",
    "delete_remote",
    "send",
    "ac",
)
# Appliance kinds the platform offers when a remote is added.
KINDS = (
    "tv",
    "ac",
    "fan",
    "light",
    "projector",
    "speaker",
    "settop",
    "dehumidifier",
    "purifier",
    "heater",
    "curtain",
    "other",
)
_AVATAR_KINDS = (
    ("dehumid", "dehumidifier"),
    ("purif", "purifier"),
    ("heater", "heater"),
    ("projector", "projector"),
    ("curtain", "curtain"),
    ("speaker", "speaker"),
    ("sound", "speaker"),
    ("audio", "speaker"),
    ("stb", "settop"),
    ("box", "settop"),
    ("tv", "tv"),
    ("fan", "fan"),
    ("light", "light"),
    ("lamp", "light"),
    ("air", "ac"),
)

# Tapo AC profiles report "P1_M0_T26_S0_D0": power, mode, temperature, wind
# speed, wind direction. Only the values users confirmed on real hubs are offered.
AC_FIELDS = ("P", "M", "T", "S", "D")
AC_MODES = {"cool": 0, "heat": 1}
AC_FANS = {"auto": 0, "low": 1, "high": 3}
AC_SWINGS = {"auto": 0, "fixed": 1}
AC_TEMP_MIN, AC_TEMP_MAX = 16, 30
# python-kasa SmartErrorCode values: login failures, a request the firmware does
# not know, and session errors after which the H110 did not run the request.
AUTH_ERROR_CODES = frozenset({-1501, 1111, 1100, 1003})
UNKNOWN_METHOD = -1002
SESSION_ERROR_CODES = frozenset({9999, -40401, -40413})

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SPACE = re.compile(r"\s+")
_OPAQUE = re.compile(r"^(?=.{8}$)(?=.*[A-Z])(?=.*[a-z])(?=.*\d)[A-Za-z0-9]+$")
_MAC = re.compile(r"^[0-9a-f]{12}$")
_KEY_ALIASES = {
    "VOL+": "Volume Up",
    "VOL-": "Volume Down",
    "CH+": "Channel Up",
    "CH-": "Channel Down",
    "TEMP+": "Temperature Up",
    "TEMP-": "Temperature Down",
    "NAVIGATE_UP": "Up",
    "NAVIGATE_DOWN": "Down",
    "NAVIGATE_LEFT": "Left",
    "NAVIGATE_RIGHT": "Right",
    "HOMEPAGE": "Home",
}
_ACRONYMS = {"AC", "AV", "EPG", "HDMI", "IR", "OK", "PC", "TV", "USB"}


class TapoIrError(Exception):
    """A failure with a code and a short message the platform shows as is."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def unsupported() -> TapoIrError:
    return TapoIrError(
        "tapo_ir_unsupported", "主機缺少 TP-Link 元件，請更新 Home Assistant"
    )


def invalid(message: str = "資料不完整，請重新操作") -> TapoIrError:
    return TapoIrError("tapo_ir_invalid", message)


def not_found(message: str = "找不到這個遙控器，請重新整理") -> TapoIrError:
    return TapoIrError("tapo_ir_not_found", message)


def failed(message: str = "H110 沒有完成，請再試一次") -> TapoIrError:
    return TapoIrError("tapo_ir_failed", message)


def unreachable() -> TapoIrError:
    return TapoIrError("tapo_ir_unreachable", "連不到 H110，請確認它和主機在同一個網路")


def auth_failed() -> TapoIrError:
    return TapoIrError("tapo_ir_auth", "Tapo 帳號或密碼不符，請用綁定這台 H110 的帳號")


def account_required() -> TapoIrError:
    """The H110 joined a Tapo account, so linking without one is not enough."""
    return TapoIrError(
        "tapo_ir_account_required", "這台 H110 綁定了 Tapo 帳號，請登入一次"
    )


def busy() -> TapoIrError:
    return TapoIrError("tapo_ir_busy", "正在學習按鍵，完成後再操作")


# --- pure helpers -----------------------------------------------------------


def clean_text(value: Any) -> str:
    """NFC text without control bytes or repeated whitespace.

    NFC, not NFKC: a name typed as 「音量＋」 must come back the same.
    """
    if value is None:
        return ""
    text = unicodedata.normalize("NFC", str(value))
    return _SPACE.sub(" ", _CONTROL.sub(" ", text)).strip()


def b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def decode_text(value: Any) -> str:
    """Decode the H110's base64 names; anything else stays readable text."""
    raw = clean_text(value)
    if not raw:
        return ""
    try:
        decoded = clean_text(base64.b64decode(raw, validate=True).decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return raw
    if not decoded or sum(c.isprintable() for c in decoded) / len(decoded) < 0.9:
        return raw
    return decoded


def normalize_mac(value: Any) -> str:
    compact = re.sub(r"[^0-9a-f]", "", str(value or "").lower())
    if not _MAC.fullmatch(compact):
        return ""
    return ":".join(compact[index : index + 2] for index in range(0, 12, 2))


def valid_host(value: Any) -> str:
    try:
        return str(IPv4Address(str(value or "").strip()))
    except ValueError:
        return ""


def mask_account(username: str) -> str:
    """``nick@gmail.com`` → ``n•••@gmail.com``; enough to recognise the account."""
    name, at, domain = clean_text(username).partition("@")
    if not name:
        return ""
    shown = name[0] + "•••"
    return f"{shown}@{domain}" if at else shown


def hub_model(model: Any) -> str:
    """``H110(EU)`` → ``H110``."""
    return clean_text(model).split("(")[0].strip().upper()


def is_ir_hub(model: Any) -> bool:
    return hub_model(model).startswith(IR_HUB_MODELS)


def discovery_candidate(raw: Any) -> dict[str, Any] | None:
    """An IR hub from python-kasa's raw discovery answer, else None."""
    if not isinstance(raw, dict):
        return None
    response = raw.get("discovery_response")
    if not isinstance(response, dict):
        return None
    result = (
        response.get("result") if isinstance(response.get("result"), dict) else response
    )
    meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
    if result.get("device_type") != HUB_FAMILY or not is_ir_hub(
        result.get("device_model")
    ):
        return None
    mac = normalize_mac(result.get("mac"))
    host = valid_host(meta.get("ip") or result.get("ip"))
    if not mac or not host:
        return None
    scheme = result.get("mgt_encrypt_schm")
    scheme = scheme if isinstance(scheme, dict) else {}
    connection = dict(DEFAULT_CONNECTION)
    if isinstance(scheme.get("encrypt_type"), str) and scheme["encrypt_type"]:
        connection["encryption_type"] = scheme["encrypt_type"]
    if isinstance(scheme.get("lv"), int):
        connection["login_version"] = scheme["lv"]
    if isinstance(scheme.get("is_support_https"), bool):
        connection["https"] = scheme["is_support_https"]
    if isinstance(scheme.get("http_port"), int):
        connection["http_port"] = scheme["http_port"]
    return {
        "id": mac,
        "mac": mac,
        "host": host,
        "model": hub_model(result.get("device_model")),
        "connection": connection,
    }


def _title(value: str) -> str:
    words = []
    for word in value.replace("_", " ").split():
        upper = word.upper()
        words.append(upper if upper in _ACRONYMS else word.capitalize())
    return " ".join(words)


def key_label(key: dict[str, Any], position: int) -> str:
    """The name a person gave the button, else a readable protocol name."""
    decoded = decode_text(key.get("display_name"))
    if decoded and not _OPAQUE.fullmatch(decoded):
        return decoded[:LABEL_MAX]
    name = clean_text(key.get("name"))
    if alias := _KEY_ALIASES.get(name.upper()):
        return alias
    if name and not _OPAQUE.fullmatch(name) and not name.lower().startswith("custom_"):
        return _title(name)[:LABEL_MAX]
    return f"按鍵 {position}"


def kind_from_avatar(avatar: Any, model: Any) -> str:
    if clean_text(model).upper() == "AC":
        return "ac"
    text = clean_text(avatar).lower()
    for hint, kind in _AVATAR_KINDS:
        if hint in text:
            return kind
    return ""


def parse_ac_status(child: dict[str, Any]) -> dict[str, int]:
    """Read the compact AC state, falling back to the separate fields."""
    state: dict[str, int] = {}
    if isinstance(status := child.get("ac_status"), str):
        for part in status.split("_"):
            if len(part) >= 2 and part[0] in AC_FIELDS:
                with contextlib.suppress(ValueError):
                    state[part[0]] = int(part[1:])
    fallbacks = {
        "P": child.get("on"),
        "M": child.get("ac_mode"),
        "T": child.get("current_temp"),
        "S": child.get("wind_speed"),
        "D": child.get("wind_direct"),
    }
    for field, value in fallbacks.items():
        if field in state or value is None or isinstance(value, float):
            continue
        with contextlib.suppress(TypeError, ValueError):
            state[field] = int(value)
    if state.get("P") not in (None, 0, 1):
        state.pop("P")
    return state


def _reverse(table: dict[str, int], value: Any) -> str | None:
    return next((name for name, code in table.items() if code == value), None)


def ac_view(state: dict[str, int]) -> dict[str, Any]:
    return {
        "power": state.get("P") == 1 if "P" in state else None,
        "mode": _reverse(AC_MODES, state.get("M")),
        "temp": state.get("T"),
        "fan": _reverse(AC_FANS, state.get("S")),
        "swing": _reverse(AC_SWINGS, state.get("D")),
        "complete": all(field in state for field in AC_FIELDS),
        "modes": list(AC_MODES),
        "fans": list(AC_FANS),
        "swings": list(AC_SWINGS),
        "min": AC_TEMP_MIN,
        "max": AC_TEMP_MAX,
    }


def apply_ac_changes(state: dict[str, int], changes: dict[str, Any]) -> dict[str, int]:
    """A complete AC state with the requested changes, or a clear refusal."""
    result = dict(state)
    if "power" in changes and changes["power"] is not None:
        result["P"] = 1 if changes["power"] else 0
    if (mode := changes.get("mode")) is not None:
        if mode not in AC_MODES:
            raise invalid("冷氣不支援這個模式")
        result["M"] = AC_MODES[mode]
        result["P"] = 1
    if (temp := changes.get("temp")) is not None:
        if isinstance(temp, bool) or not isinstance(temp, int):
            raise invalid("溫度要是整數")
        if not AC_TEMP_MIN <= temp <= AC_TEMP_MAX:
            raise invalid(f"溫度要在 {AC_TEMP_MIN}–{AC_TEMP_MAX}°")
        result["T"] = temp
    if (fan := changes.get("fan")) is not None:
        if fan not in AC_FANS:
            raise invalid("冷氣不支援這個風速")
        result["S"] = AC_FANS[fan]
    if (swing := changes.get("swing")) is not None:
        if swing not in AC_SWINGS:
            raise invalid("冷氣不支援這個風向")
        result["D"] = AC_SWINGS[swing]
    if any(field not in result for field in AC_FIELDS):
        raise TapoIrError(
            "tapo_ir_ac_state", "這台冷氣還沒有完整狀態，請先用原廠遙控器按一次"
        )
    return result


def ac_payload(state: dict[str, int]) -> dict[str, Any]:
    return {
        "power": bool(state["P"]),
        "on": bool(state["P"]),
        "mode": state["M"],
        "temp": state["T"],
        "wind_speed": state["S"],
        "wind_direct": state["D"],
    }


def remote_summary(child: dict[str, Any], kind: str = "") -> dict[str, Any]:
    """What the platform shows for one remote; IR waveforms stay on the hub."""
    device_id = str(child.get("device_id") or "")
    model = clean_text(child.get("model"))
    keys = []
    for position, key in enumerate(child.get("key_list") or [], start=1):
        if not isinstance(key, dict) or not clean_text(key.get("name")):
            continue
        keys.append(
            {
                "name": clean_text(key.get("name")),
                "label": key_label(key, position),
                "order": key.get("order")
                if isinstance(key.get("order"), int)
                else position,
            }
        )
    keys.sort(key=lambda item: item["order"])
    name = decode_text(child.get("nickname"))
    if not name or _OPAQUE.fullmatch(name):
        name = f"遙控器 {device_id[-4:]}"
    return {
        "id": device_id,
        "name": name[:NAME_MAX],
        "kind": kind or kind_from_avatar(child.get("avatar"), model),
        "model": model,
        "brand": decode_text(child.get("brand")),
        "learned": model.lower() == "custom",
        "keys": keys,
        "ac": ac_view(parse_ac_status(child)) if model.upper() == "AC" else None,
    }


def slug(text: str) -> str:
    value = clean_text(text).lower().replace("+", " plus ").replace("-", " minus ")
    return re.sub(r"[^a-z0-9]+", "_", value).strip("_") or "key"


def new_key_name(keys: list[dict[str, Any]], label: str) -> str:
    existing = {str(key.get("name", "")).casefold() for key in keys}
    stem = f"custom_{slug(label)}"
    candidate, suffix = stem, 2
    while candidate.casefold() in existing:
        candidate, suffix = f"{stem}_{suffix}", suffix + 1
    return candidate


def new_remote_payload(source: dict[str, Any] | None, nickname: str) -> dict[str, Any]:
    """addIrRemoteDevice for an empty learned remote, copying the hub's own fields."""
    source = source or {}
    return {
        "device_type": source.get("device_type", "SMART.TAPOREMOTE"),
        "nickname": b64(nickname),
        "avatar": "remote",
        "category": IR_CATEGORY,
        "model": "Custom",
        "key_sum": 0,
        "brand": None,
        "key_list": [],
        "copy_device_id": None,
        "hexData": None,
        "remote_type": 1,
        "remote_id": None,
        "frequency": source.get("frequency"),
    }


def error_number(code: Any) -> int | None:
    value = getattr(code, "value", code)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def check_code(code: Any, method: str) -> None:
    value = getattr(code, "value", code)
    if value in (None, 0, "0"):
        return
    number = error_number(code)
    if number in AUTH_ERROR_CODES:
        raise auth_failed()
    if number == UNKNOWN_METHOD:
        raise TapoIrError(
            "tapo_ir_firmware", "這台 H110 的韌體不支援，請先在 Tapo App 更新韌體"
        )
    raise failed(f"H110 拒絕了 {method}（{value}），請再試一次")


def unwrap(response: Any, method: str) -> dict[str, Any]:
    """python-kasa answers ``{method: result}``; set_ methods return no result."""
    if isinstance(response, dict) and method in response:
        response = response[method]
    if response is None:
        return {}
    if isinstance(response, dict):
        check_code(response.get("error_code"), method)
        return response
    check_code(response, method)
    return {}


def child_request(
    device_id: str, method: str, params: Any = None, *, batched: bool = False
) -> dict[str, Any]:
    """The ``control_child`` envelope python-kasa's child wrapper would build."""
    if batched:
        request: dict[str, Any] = {
            "method": "multipleRequest",
            "params": {
                "requests": [
                    {"method": method, "params": params}
                    if params is not None
                    else {"method": method}
                ]
            },
        }
    else:
        request = {"method": method, "params": params}
    return {"control_child": {"device_id": device_id, "requestData": request}}


def child_result(response: Any, method: str) -> dict[str, Any]:
    """Unwrap ``control_child`` (and its multipleRequest) and check every code."""
    result = response.get("control_child") if isinstance(response, dict) else None
    if not isinstance(result, dict):
        return {}
    data = result.get("responseData")
    if not isinstance(data, dict):
        return result
    check_code(data.get("error_code"), method)
    payload = data.get("result")
    if isinstance(payload, dict) and isinstance(payload.get("responses"), list):
        found: dict[str, Any] = {}
        for item in payload["responses"]:
            if not isinstance(item, dict):
                continue
            check_code(item.get("error_code"), str(item.get("method") or method))
            if item.get("method") == method and isinstance(item.get("result"), dict):
                found = item["result"]
        return found
    return payload if isinstance(payload, dict) else {}


def remote_record(response: dict[str, Any]) -> dict[str, Any]:
    """One remote's own ``get_device_info``, wherever the hub nests it."""
    stack: list[Any] = [response]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if item.get("category") == IR_CATEGORY or "key_list" in item:
                return item
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    raise failed("H110 沒有回傳遙控器資料，請再試一次")


def valid_code(status: dict[str, Any]) -> dict[str, Any] | None:
    pwm, pulse = status.get("pwm"), status.get("pulse")
    if isinstance(pwm, bool) or not isinstance(pwm, int) or pwm <= 0:
        return None
    if not isinstance(pulse, str) or not pulse.strip():
        return None
    return {"pwm": pwm, "pulse": pulse}


def saved_key(
    remote: dict[str, Any],
    wanted: dict[str, Any],
    before: set[str],
    label: str,
) -> dict[str, Any]:
    """The key the hub kept, even when it renamed the provisional key name."""
    keys = [key for key in remote.get("key_list") or [] if isinstance(key, dict)]
    exact = [key for key in keys if key.get("name") == wanted["name"]]
    candidates = exact or [
        key
        for key in keys
        if key.get("name") not in before
        and key.get("pwm") == wanted["pwm"]
        and decode_text(key.get("display_name")) == label
    ]
    if len(candidates) != 1:
        raise failed("H110 沒有存下這個按鍵，請再學一次")
    key = candidates[0]
    pulse = key.get("pulse")
    if (
        key.get("pwm") != wanted["pwm"]
        or decode_text(key.get("display_name")) != label
        or (pulse is not None and str(pulse) != wanted["pulse"])
    ):
        raise failed("H110 存的按鍵和學到的不一致，請再學一次")
    return key


def clean_name(value: Any, limit: int, message: str) -> str:
    text = clean_text(value)
    if not text or len(text) > limit:
        raise invalid(message)
    return text


def load_kasa() -> SimpleNamespace | None:
    """python-kasa as Home Assistant's TP-Link integration ships it, if present."""
    try:
        from kasa import (
            AuthenticationError,
            Credentials,
            DeviceConfig,
            DeviceConnectionParameters,
            DeviceError,
            Discover,
            KasaException,
        )
        from kasa.device_factory import get_protocol
    except Exception:  # noqa: BLE001 - any import problem means "not available"
        return None
    return SimpleNamespace(
        AuthenticationError=AuthenticationError,
        Credentials=Credentials,
        DeviceConfig=DeviceConfig,
        DeviceConnectionParameters=DeviceConnectionParameters,
        DeviceError=DeviceError,
        Discover=Discover,
        KasaException=KasaException,
        get_protocol=get_protocol,
    )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# --- one H110 -----------------------------------------------------------------


class HubClient:
    """One KLAP session to one H110; the hub handles one request at a time."""

    def __init__(self, kasa: SimpleNamespace, record: dict[str, Any]) -> None:
        self._kasa = kasa
        self.record = record
        self._protocol: Any = None
        self._credentials: Any = None
        self.lock = asyncio.Lock()

    def use_credentials(self, username: str, password: str) -> None:
        self._credentials = self._kasa.Credentials(username, password)

    def _config(self) -> Any:
        kasa = self._kasa
        connection = kasa.DeviceConnectionParameters.from_dict(
            self.record.get("connection") or DEFAULT_CONNECTION
        )
        config = kasa.DeviceConfig(
            self.record["host"], timeout=REQUEST_TIMEOUT, connection_type=connection
        )
        if self._credentials is not None:
            config.credentials = self._credentials
        elif self.record.get("credentials_hash"):
            config.credentials_hash = self.record["credentials_hash"]
        # Neither (linked without an account): python-kasa uses blank credentials
        # and falls back to TP-Link's setup credentials.
        return config

    def _ensure(self) -> Any:
        if self._protocol is None:
            protocol = self._kasa.get_protocol(self._config())
            if protocol is None:
                raise unsupported()
            self._protocol = protocol
        return self._protocol

    @property
    def credentials_hash(self) -> str | None:
        transport = getattr(self._protocol, "_transport", None)
        value = getattr(transport, "credentials_hash", None)
        return value if isinstance(value, str) and value else None

    async def _send(self, request: Any, *, mutation: bool) -> Any:
        kasa = self._kasa
        for attempt in range(2):
            protocol = self._ensure()
            try:
                async with asyncio.timeout(REQUEST_TIMEOUT * 3):
                    # A retried write could press a button twice or add two
                    # remotes, so python-kasa only retries reads.
                    return await protocol.query(
                        request, retry_count=0 if mutation else 2
                    )
            except kasa.AuthenticationError as err:
                await self.close()
                raise auth_failed() from err
            except kasa.DeviceError as err:
                code = getattr(err, "error_code", None)
                if attempt == 0 and error_number(code) in SESSION_ERROR_CODES:
                    # The session expired before the H110 ran it: log in again.
                    await self.close()
                    continue
                check_code(code, "request")
                raise failed() from err
            except (kasa.KasaException, OSError, TimeoutError) as err:
                await self.close()
                raise unreachable() from err
        raise unreachable()

    async def hub(self, method: str, params: Any = None) -> dict[str, Any]:
        mutation = not method.startswith("get")
        response = await self._send({method: params}, mutation=mutation)
        return unwrap(response, method)

    async def child(
        self, device_id: str, method: str, params: Any = None, *, batched: bool = False
    ) -> dict[str, Any]:
        mutation = not method.startswith("get")
        response = await self._send(
            child_request(device_id, method, params, batched=batched), mutation=mutation
        )
        return child_result(response, method)

    async def children(self) -> list[dict[str, Any]]:
        """Every IR remote; python-kasa follows the list's pages itself."""
        result = await self.hub("get_child_device_list")
        page = result.get("child_device_list")
        if not isinstance(page, list):
            raise failed("H110 沒有回傳遙控器清單，請再試一次")
        return [
            child
            for child in page
            if isinstance(child, dict)
            and child.get("category") == IR_CATEGORY
            and child.get("device_id")
        ]

    async def remote(self, device_id: str) -> dict[str, Any]:
        return remote_record(await self.child(device_id, "get_device_info"))

    async def close(self) -> None:
        protocol, self._protocol = self._protocol, None
        if protocol is not None:
            with contextlib.suppress(Exception):
                await protocol.close()


class Capture:
    """One learning session: the H110 waits for a button from the original remote."""

    def __init__(self, timeout: int) -> None:
        self.state = "waiting"
        self.error = ""
        self.capture_id = ""
        self.deadline = time.monotonic() + timeout
        self.stop = asyncio.Event()
        self.task: asyncio.Task | None = None

    def view(self) -> dict[str, Any]:
        view: dict[str, Any] = {"state": self.state}
        if self.state == "waiting":
            view["remaining"] = max(0, round(self.deadline - time.monotonic()))
        if self.capture_id:
            view["captureId"] = self.capture_id
        if self.error:
            view["error"] = self.error
        return view


# --- the command --------------------------------------------------------------


class TapoIrManager:
    """Linked H110s, their KLAP sessions and learning sessions."""

    def __init__(
        self,
        store: Any,
        kasa_loader: Any = load_kasa,
        broadcast_addresses: Any = None,
        create_task: Any = None,
    ) -> None:
        self._store = store
        self._kasa_loader = kasa_loader
        self._kasa: SimpleNamespace | None = None
        self._kasa_loaded = False
        self._broadcast_addresses = broadcast_addresses
        self._create_task = create_task or asyncio.create_task
        self._data: dict[str, Any] | None = None
        self._clients: dict[str, HubClient] = {}
        self._captures: dict[str, Capture] = {}
        self._codes: dict[str, dict[str, Any]] = {}
        self._found: dict[str, dict[str, Any]] = {}

    # storage
    async def _load(self) -> dict[str, Any]:
        if self._data is None:
            stored = await self._store.async_load()
            data = stored if isinstance(stored, dict) else {}
            hubs = data.get("hubs") if isinstance(data.get("hubs"), dict) else {}
            kinds = data.get("kinds") if isinstance(data.get("kinds"), dict) else {}
            self._data = {"hubs": hubs, "kinds": kinds}
        return self._data

    async def _save(self) -> None:
        await self._store.async_save(self._data)

    def kasa(self) -> SimpleNamespace:
        if not self._kasa_loaded:
            self._kasa = self._kasa_loader()
            self._kasa_loaded = True
        if self._kasa is None:
            raise unsupported()
        return self._kasa

    def supported(self) -> bool:
        try:
            self.kasa()
        except TapoIrError:
            return False
        return True

    async def _record(self, hub: Any) -> dict[str, Any]:
        data = await self._load()
        record = data["hubs"].get(normalize_mac(hub))
        if record is None:
            raise TapoIrError("tapo_ir_not_linked", "這台 H110 還沒連結，請重新連結")
        return record

    def _client(self, record: dict[str, Any]) -> HubClient:
        client = self._clients.get(record["mac"])
        if client is None or client.record is not record:
            client = self._clients[record["mac"]] = HubClient(self.kasa(), record)
        return client

    def _learning(self, mac: str) -> bool:
        capture = self._captures.get(mac)
        return capture is not None and capture.state == "waiting"

    @staticmethod
    def _hub_view(record: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": record["mac"],
            "name": record.get("name") or "Tapo H110",
            "model": record.get("model") or "H110",
            "host": record.get("host", ""),
            "account": record.get("account", ""),
            "linkedAt": record.get("linkedAt", ""),
        }

    # discovery
    async def discover(self) -> list[dict[str, Any]]:
        kasa = self.kasa()
        targets = ["255.255.255.255"]
        if self._broadcast_addresses is not None:
            with contextlib.suppress(Exception):
                targets = [str(item) for item in await self._broadcast_addresses()]
                targets = targets or ["255.255.255.255"]
        found: dict[str, dict[str, Any]] = {}

        def on_raw(raw: Any) -> None:
            if candidate := discovery_candidate(raw):
                found[candidate["mac"]] = candidate

        results = await asyncio.gather(
            *(
                kasa.Discover.discover(
                    target=target,
                    discovery_timeout=DISCOVERY_TIMEOUT,
                    on_discovered_raw=on_raw,
                )
                for target in targets
            ),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, dict):
                for device in result.values():
                    with contextlib.suppress(Exception):
                        await device.disconnect()
        self._found = found
        await self._follow_moved_hubs(found)
        return sorted(found.values(), key=lambda item: item["host"])

    async def _follow_moved_hubs(self, found: dict[str, dict[str, Any]]) -> None:
        """A linked H110 the router gave a new address keeps working."""
        data = await self._load()
        moved = False
        for mac, candidate in found.items():
            record = data["hubs"].get(mac)
            if record and record.get("host") != candidate["host"]:
                record["host"] = candidate["host"]
                record["connection"] = candidate["connection"]
                moved = True
                if client := self._clients.pop(mac, None):
                    await client.close()
        if moved:
            await self._save()

    async def _with_client(self, record: dict[str, Any], job: Any) -> Any:
        """Run one job; if the H110 moved, find it again and retry once."""
        try:
            return await self._run_job(record, job)
        except TapoIrError as err:
            if err.code == "tapo_ir_auth" and not record.get("credentials_hash"):
                # Linked without an account, and now the H110 wants one.
                raise account_required() from err
            raise

    async def _run_job(self, record: dict[str, Any], job: Any) -> Any:
        client = self._client(record)
        try:
            async with client.lock:
                return await job(client)
        except TapoIrError as err:
            if err.code != "tapo_ir_unreachable":
                raise
            host = record.get("host")
            await self.discover()
            if record.get("host") == host:
                raise
        client = self._client(record)
        async with client.lock:
            return await job(client)

    # actions
    async def hubs(self, msg: dict[str, Any]) -> dict[str, Any]:
        if not self.supported():
            return {"supported": False, "hubs": [], "found": []}
        data = await self._load()
        found: list[dict[str, Any]] = []
        if msg.get("discover"):
            found = [
                {key: item[key] for key in ("id", "mac", "host", "model")}
                for item in await self.discover()
                if item["mac"] not in data["hubs"]
            ]
        return {
            "supported": True,
            "hubs": [self._hub_view(record) for record in data["hubs"].values()],
            "found": found,
        }

    async def link(self, msg: dict[str, Any]) -> dict[str, Any]:
        kasa = self.kasa()
        host = valid_host(msg.get("host"))
        username = clean_text(msg.get("username"))
        password = str(msg.get("password") or "")
        if not host:
            raise invalid("請輸入 H110 的 IP 位址")
        # No account at all = try blank / TP-Link setup credentials first.
        with_account = bool(username or password)
        if with_account and (not username or len(username) > ACCOUNT_MAX):
            raise invalid("請輸入 Tapo 帳號（電子郵件）")
        if with_account and (not password or len(password) > ACCOUNT_MAX):
            raise invalid("請輸入 Tapo 密碼")
        known = next(
            (item for item in self._found.values() if item["host"] == host), None
        )
        record: dict[str, Any] = {
            "host": host,
            "mac": known["mac"] if known else "",
            "connection": dict(known["connection"])
            if known
            else dict(DEFAULT_CONNECTION),
        }
        client = HubClient(kasa, record)
        if with_account:
            client.use_credentials(username, password)
        try:
            info = await client.hub("get_device_info")
            credentials_hash = client.credentials_hash if with_account else ""
            remotes = await client.children()
        except TapoIrError as err:
            if err.code == "tapo_ir_auth" and not with_account:
                raise account_required() from err
            raise
        finally:
            await client.close()
        mac = normalize_mac(info.get("mac")) or record["mac"]
        if info.get("type") not in (None, HUB_FAMILY) or not is_ir_hub(
            info.get("model")
        ):
            raise invalid("這台不是 Tapo H110，請確認 IP 位址")
        if not mac or (with_account and not credentials_hash):
            raise failed("H110 沒有完成登入，請再試一次")
        data = await self._load()
        record.update(
            {
                "mac": mac,
                "model": hub_model(info.get("model")),
                "name": decode_text(info.get("nickname")) or "Tapo H110",
                # Empty hash and account = linked without an account.
                "credentials_hash": credentials_hash,
                "account": mask_account(username) if with_account else "",
                "linkedAt": _now(),
            }
        )
        if old := self._clients.pop(mac, None):
            await old.close()
        data["hubs"][mac] = record
        await self._save()
        return {"hub": self._hub_view(record), "remotes": self._views(remotes)}

    async def unlink(self, msg: dict[str, Any]) -> dict[str, Any]:
        record = await self._record(msg.get("hub"))
        data = await self._load()
        data["hubs"].pop(record["mac"], None)
        if client := self._clients.pop(record["mac"], None):
            await client.close()
        await self._save()
        return {"unlinked": record["mac"]}

    def _views(self, children: list[dict[str, Any]]) -> list[dict[str, Any]]:
        kinds = (self._data or {}).get("kinds", {})
        views = [
            remote_summary(child, kinds.get(str(child["device_id"]), ""))
            for child in children
        ]
        return sorted(views, key=lambda item: item["name"].casefold())

    async def remotes(self, msg: dict[str, Any]) -> dict[str, Any]:
        record = await self._record(msg.get("hub"))
        children = await self._with_client(record, lambda client: client.children())
        return {"hub": self._hub_view(record), "remotes": self._views(children)}

    async def learn(self, msg: dict[str, Any]) -> dict[str, Any]:
        record = await self._record(msg.get("hub"))
        mac = record["mac"]
        if self._learning(mac):
            return self._captures[mac].view()
        timeout = msg.get("timeout", LEARN_TIMEOUT)
        if isinstance(timeout, bool) or not isinstance(timeout, int):
            raise invalid()
        capture = Capture(max(LEARN_MIN, min(LEARN_MAX, timeout)))
        self._captures[mac] = capture
        capture.task = self._create_task(self._run_capture(record, capture))
        return capture.view()

    async def _run_capture(self, record: dict[str, Any], capture: Capture) -> None:
        client = self._client(record)
        try:
            async with client.lock:
                await client.hub("startIrReceiveMode")
                try:
                    await self._wait_for_signal(client, capture)
                finally:
                    with contextlib.suppress(Exception):
                        async with asyncio.timeout(STOP_TIMEOUT):
                            await client.hub("stopIrReceiveMode")
        except TapoIrError as err:
            capture.state, capture.error = "failed", err.message
        except asyncio.CancelledError:
            capture.state = "stopped"
            raise
        except Exception:  # noqa: BLE001 - a learning failure must not reach HA
            _LOGGER.exception("Tapo IR learning failed")
            capture.state, capture.error = "failed", "學習中斷，請再試一次"

    async def _wait_for_signal(self, client: HubClient, capture: Capture) -> None:
        while True:
            if capture.stop.is_set():
                capture.state = "stopped"
                return
            status = await client.hub("getIrReceiveStatus")
            received = status.get("recv_status")
            if received == 0 and (code := valid_code(status)):
                capture.capture_id = self._keep_code(client.record["mac"], code)
                capture.state = "captured"
                return
            if received == -2:
                capture.state = "failed"
                capture.error = "收到的訊號無法使用，請再按一次"
                return
            if time.monotonic() >= capture.deadline:
                capture.state = "timeout"
                return
            await asyncio.sleep(LEARN_POLL_SECONDS)

    def _keep_code(self, mac: str, code: dict[str, Any]) -> str:
        now = time.monotonic()
        for capture_id, item in list(self._codes.items()):
            if now - item["at"] > CAPTURE_TTL:
                self._codes.pop(capture_id)
        while len(self._codes) >= CAPTURE_LIMIT:
            self._codes.pop(next(iter(self._codes)))
        capture_id = secrets.token_hex(8)
        self._codes[capture_id] = {"mac": mac, "code": code, "at": now}
        return capture_id

    async def learn_status(self, msg: dict[str, Any]) -> dict[str, Any]:
        record = await self._record(msg.get("hub"))
        capture = self._captures.get(record["mac"])
        return capture.view() if capture else {"state": "idle"}

    async def learn_stop(self, msg: dict[str, Any]) -> dict[str, Any]:
        record = await self._record(msg.get("hub"))
        capture = self._captures.get(record["mac"])
        if capture is None:
            return {"state": "idle"}
        capture.stop.set()
        if capture.task is not None:
            with contextlib.suppress(Exception):
                async with asyncio.timeout(STOP_TIMEOUT + 2):
                    await asyncio.shield(capture.task)
        return capture.view()

    def _code(self, mac: str, capture_id: Any) -> dict[str, Any]:
        item = self._codes.get(str(capture_id or ""))
        if item is None or item["mac"] != mac:
            raise TapoIrError("tapo_ir_capture_expired", "學到的按鍵已過期，請重新學習")
        if time.monotonic() - item["at"] > CAPTURE_TTL:
            self._codes.pop(str(capture_id), None)
            raise TapoIrError("tapo_ir_capture_expired", "學到的按鍵已過期，請重新學習")
        return item["code"]

    async def save_key(self, msg: dict[str, Any]) -> dict[str, Any]:
        record = await self._record(msg.get("hub"))
        if self._learning(record["mac"]):
            raise busy()
        capture_id = str(msg.get("captureId") or "")
        code = self._code(record["mac"], capture_id)
        label = clean_name(msg.get("label"), LABEL_MAX, "請替按鍵取名")
        remote_id = clean_text(msg.get("remote"))
        kind = clean_text(msg.get("kind"))
        if kind and kind not in KINDS:
            raise invalid("請選擇家電類型")
        if remote_id:

            async def job(client: HubClient) -> dict[str, Any]:
                await self._write_key(
                    client, remote_id, clean_text(msg.get("key")), label, code
                )
                return await client.remote(remote_id)

        else:
            name = clean_name(msg.get("name"), NAME_MAX, "請替遙控器取名")

            async def job(client: HubClient) -> dict[str, Any]:
                created = await self._create_remote(client, name, label, code)
                return await client.remote(created)

        child = await self._with_client(record, job)
        self._codes.pop(capture_id, None)
        data = await self._load()
        if kind:
            data["kinds"][str(child["device_id"])] = kind
            await self._save()
        return {"remote": self._views([child])[0]}

    async def _write_key(
        self,
        client: HubClient,
        remote_id: str,
        key_name: str,
        label: str,
        code: dict[str, Any],
    ) -> dict[str, Any]:
        remote = await client.remote(remote_id)
        keys = [key for key in remote.get("key_list") or [] if isinstance(key, dict)]
        before = {str(key.get("name")) for key in keys}
        if key_name:
            match = next((key for key in keys if key.get("name") == key_name), None)
            if match is None:
                raise not_found("找不到這個按鍵，請重新整理")
            key = deepcopy(match)
        else:
            key = {
                "name": new_key_name(keys, label),
                "icon": "",
                "order": len(keys) + 1,
                "type": "",
            }
        key.pop("enable", None)
        key.update(
            {
                "id": -1,
                "display_name": b64(label),
                "pwm": code["pwm"],
                "pulse": code["pulse"],
            }
        )
        await client.child(
            remote_id, "setKeyInfo", {"delete_key_list": [], "edit_key_list": [key]}
        )
        return saved_key(await client.remote(remote_id), key, before, label)

    async def _create_remote(
        self, client: HubClient, name: str, label: str, code: dict[str, Any]
    ) -> str:
        """A new remote with its first button; never leaves an empty one behind."""
        before = await client.children()
        before_ids = {str(child["device_id"]) for child in before}
        marker = f" [MX-{secrets.token_hex(3)}]"
        provisional = f"{name[: NAME_MAX - len(marker)]}{marker}"
        response = await client.hub(
            "addIrRemoteDevice",
            new_remote_payload(before[0] if before else None, provisional),
        )
        after = await client.children()
        new = [
            child
            for child in after
            if str(child["device_id"]) not in before_ids
            and decode_text(child.get("nickname")) == provisional
        ]
        if len(new) != 1:
            for child in new:
                with contextlib.suppress(TapoIrError):
                    await client.child(str(child["device_id"]), "deleteRemote")
            _LOGGER.warning("Tapo IR remote not identified after create: %s", response)
            raise failed("H110 沒有建立遙控器，請再試一次")
        created = str(new[0]["device_id"])
        try:
            await self._write_key(client, created, "", label, code)
            await client.child(created, "setDeviceInfo", {"nickname": b64(name)})
            if decode_text((await client.remote(created)).get("nickname")) != name:
                raise failed("H110 沒有存下遙控器名稱，請再試一次")
        except BaseException:
            with contextlib.suppress(Exception):
                await client.child(created, "deleteRemote")
            raise
        return created

    async def delete_key(self, msg: dict[str, Any]) -> dict[str, Any]:
        record = await self._record(msg.get("hub"))
        if self._learning(record["mac"]):
            raise busy()
        remote_id = clean_text(msg.get("remote"))
        key_name = clean_text(msg.get("key"))
        if not remote_id or not key_name:
            raise invalid()

        async def job(client: HubClient) -> dict[str, Any]:
            remote = await client.remote(remote_id)
            if not any(
                key.get("name") == key_name for key in remote.get("key_list") or []
            ):
                raise not_found("找不到這個按鍵，請重新整理")
            await client.child(
                remote_id,
                "setKeyInfo",
                {"delete_key_list": [{"name": key_name}], "edit_key_list": []},
            )
            current = await client.remote(remote_id)
            if any(
                key.get("name") == key_name for key in current.get("key_list") or []
            ):
                raise failed("H110 沒有刪除這個按鍵，請再試一次")
            return current

        child = await self._with_client(record, job)
        return {"remote": self._views([child])[0]}

    async def rename(self, msg: dict[str, Any]) -> dict[str, Any]:
        record = await self._record(msg.get("hub"))
        if self._learning(record["mac"]):
            raise busy()
        remote_id = clean_text(msg.get("remote"))
        name = clean_name(msg.get("name"), NAME_MAX, "請替遙控器取名")
        kind = clean_text(msg.get("kind"))
        if not remote_id or (kind and kind not in KINDS):
            raise invalid()

        async def job(client: HubClient) -> dict[str, Any]:
            await client.child(remote_id, "setDeviceInfo", {"nickname": b64(name)})
            current = await client.remote(remote_id)
            if decode_text(current.get("nickname")) != name:
                raise failed("H110 沒有存下名稱，請再試一次")
            return current

        child = await self._with_client(record, job)
        if kind:
            data = await self._load()
            data["kinds"][remote_id] = kind
            await self._save()
        return {"remote": self._views([child])[0]}

    async def delete_remote(self, msg: dict[str, Any]) -> dict[str, Any]:
        record = await self._record(msg.get("hub"))
        if self._learning(record["mac"]):
            raise busy()
        remote_id = clean_text(msg.get("remote"))
        if not remote_id:
            raise invalid()

        async def job(client: HubClient) -> None:
            before = await client.children()
            if not any(str(child["device_id"]) == remote_id for child in before):
                raise not_found()
            await client.child(remote_id, "deleteRemote")
            after = await client.children()
            if any(str(child["device_id"]) == remote_id for child in after):
                raise failed("H110 沒有刪除遙控器，請再試一次")

        await self._with_client(record, job)
        data = await self._load()
        if data["kinds"].pop(remote_id, None) is not None:
            await self._save()
        return {"deleted": remote_id}

    async def send(self, msg: dict[str, Any]) -> dict[str, Any]:
        record = await self._record(msg.get("hub"))
        if self._learning(record["mac"]):
            raise busy()
        remote_id = clean_text(msg.get("remote"))
        key_name = clean_text(msg.get("key"))
        if not remote_id or not key_name:
            raise invalid()
        await self._with_client(
            record,
            lambda client: client.child(
                remote_id, "sendIrCmdById", {"name": key_name}, batched=True
            ),
        )
        return {"sent": key_name}

    async def ac(self, msg: dict[str, Any]) -> dict[str, Any]:
        record = await self._record(msg.get("hub"))
        if self._learning(record["mac"]):
            raise busy()
        remote_id = clean_text(msg.get("remote"))
        if not remote_id:
            raise invalid()
        changes = {
            field: msg.get(field)
            for field in ("power", "mode", "temp", "fan", "swing")
            if field in msg
        }

        async def job(client: HubClient) -> dict[str, Any]:
            remote = await client.remote(remote_id)
            if clean_text(remote.get("model")).upper() != "AC":
                raise invalid("這個遙控器不是冷氣")
            state = apply_ac_changes(parse_ac_status(remote), changes)
            await client.child(
                remote_id, "sendIrCmdByStatus", ac_payload(state), batched=True
            )
            return await client.remote(remote_id)

        child = await self._with_client(record, job)
        return {"remote": self._views([child])[0]}

    async def handle(self, msg: dict[str, Any]) -> dict[str, Any]:
        action = msg.get("action")
        if action not in ACTIONS:
            raise invalid()
        return await getattr(self, action)(msg)

    async def close(self) -> None:
        for capture in self._captures.values():
            capture.stop.set()
            if capture.task is not None and not capture.task.done():
                capture.task.cancel()
        for client in list(self._clients.values()):
            await client.close()
        self._clients.clear()


def register_tapo_ir(hass: Any) -> TapoIrManager:
    import voluptuous as vol
    from homeassistant.components import websocket_api
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP
    from homeassistant.helpers.storage import Store

    if DATA_KEY in hass.data:
        return hass.data[DATA_KEY]

    async def broadcast_addresses() -> list[str]:
        from homeassistant.components import network

        return [
            str(item) for item in await network.async_get_ipv4_broadcast_addresses(hass)
        ]

    manager = TapoIrManager(
        Store(hass, STORE_VERSION, STORE_KEY, private=True),
        broadcast_addresses=broadcast_addresses,
        create_task=lambda job: hass.async_create_background_task(
            job, "rexlite tapo ir learning"
        ),
    )
    hass.data[DATA_KEY] = manager

    async def stopping(_event: Any) -> None:
        await manager.close()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stopping)

    text = vol.All(str, vol.Length(max=256))

    @websocket_api.websocket_command(
        {
            vol.Required("type"): "rexlite/tapo_ir",
            vol.Required("action"): vol.In(ACTIONS),
            vol.Optional("hub"): text,
            vol.Optional("host"): text,
            vol.Optional("username"): text,
            vol.Optional("password"): text,
            vol.Optional("discover"): bool,
            vol.Optional("timeout"): vol.All(
                int, vol.Range(min=LEARN_MIN, max=LEARN_MAX)
            ),
            vol.Optional("remote"): text,
            vol.Optional("key"): text,
            vol.Optional("label"): text,
            vol.Optional("name"): text,
            vol.Optional("kind"): vol.In(KINDS),
            vol.Optional("captureId"): text,
            vol.Optional("power"): bool,
            vol.Optional("mode"): vol.In(tuple(AC_MODES)),
            vol.Optional("temp"): vol.All(
                int, vol.Range(min=AC_TEMP_MIN, max=AC_TEMP_MAX)
            ),
            vol.Optional("fan"): vol.In(tuple(AC_FANS)),
            vol.Optional("swing"): vol.In(tuple(AC_SWINGS)),
        }
    )
    @websocket_api.require_admin
    @websocket_api.async_response
    async def tapo_ir(hass, connection, msg):
        try:
            result = await manager.handle(msg)
        except TapoIrError as err:
            connection.send_error(msg["id"], err.code, err.message)
            return
        except Exception:  # noqa: BLE001 - an H110 problem must not reach the tunnel
            _LOGGER.exception("Tapo IR %s failed", msg.get("action"))
            connection.send_error(
                msg["id"], "tapo_ir_failed", "H110 沒有完成，請再試一次"
            )
            return
        connection.send_result(msg["id"], result)

    websocket_api.async_register_command(hass, tapo_ir)
    return manager
