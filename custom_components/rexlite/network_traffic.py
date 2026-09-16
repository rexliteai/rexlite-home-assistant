"""Host-local, provider-neutral traffic normalization; no credentials or I/O."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

PROVIDERS = ("tp_link", "vigi", "deco", "zyxel", "fortinet", "unifi", "generic")
RATE_UNITS = {
    "bit/s": 1,
    "kbit/s": 1000,
    "Mbit/s": 1000000,
    "Mbps": 1000000,
    "B/s": 8,
    "kB/s": 8000,
    "MB/s": 8000000,
    "KiB/s": 8192,
    "MiB/s": 8388608,
}
MAX_AGE = 120
MAX_CHANNELS = 128
ENTITY = re.compile(r"^(sensor|device_tracker)\.[a-z0-9_]+$")


def validate_channel(value: Any) -> dict[str, Any]:
    """Reject unknown fields, unsafe numeric assumptions, and missing continuity."""
    fields = {
        "id",
        "name",
        "provider",
        "scope",
        "device_id",
        "mode",
        "unit",
        "rx_entity",
        "tx_entity",
        "rx_attribute",
        "tx_attribute",
        "discontinuity_entity",
        "uptime_entity",
    }
    if (
        not isinstance(value, dict)
        or set(value) - fields
        or any(not isinstance(item, str) for item in value.values())
    ):
        raise ValueError("invalid_channel")
    result = dict(value)
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", str(result.get("id", ""))):
        raise ValueError("invalid_id")
    if (
        not isinstance(result.get("name"), str)
        or not 1 <= len(result["name"].strip()) <= 80
    ):
        raise ValueError("invalid_name")
    result["name"] = result["name"].strip()
    if result.get("provider") not in PROVIDERS:
        raise ValueError("invalid_provider")
    if result.get("scope") not in ("device", "interface", "router"):
        raise ValueError("invalid_scope")
    if result.get("scope") == "device":
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", str(result.get("device_id", ""))):
            raise ValueError("device_required")
    elif result.get("device_id"):
        raise ValueError("interface_cannot_be_device_total")
    if result.get("mode") not in ("rate", "counter"):
        raise ValueError("invalid_mode")
    for direction in ("rx", "tx"):
        if not ENTITY.fullmatch(str(result.get(f"{direction}_entity", ""))):
            raise ValueError("invalid_entity")
        attribute = result.get(f"{direction}_attribute", "")
        if not isinstance(attribute, str) or (
            attribute and not re.fullmatch(r"[a-zA-Z0-9_]{1,80}", attribute)
        ):
            raise ValueError("invalid_attribute")
    if (result["rx_entity"], result.get("rx_attribute", "")) == (
        result["tx_entity"],
        result.get("tx_attribute", ""),
    ):
        raise ValueError("duplicate_direction")
    if result["mode"] == "rate":
        if result.get("unit") not in RATE_UNITS:
            raise ValueError("invalid_rate_unit")
    else:
        if result.get("unit") != "B" or any(
            not ENTITY.fullmatch(str(result.get(key, "")))
            for key in ("discontinuity_entity", "uptime_entity")
        ):
            raise ValueError("counter_requires_bytes_discontinuity_and_uptime")
    return result


def numeric(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() and 0 <= number <= Decimal(2**64 - 1) else None


class TrafficSampler:
    """Sample fresh HA states using their timestamps; counter resets never spike."""

    def __init__(self) -> None:
        self.history: dict[tuple[str, str], tuple] = {}

    def forget(self, channel_id: str) -> None:
        for key in list(self.history):
            if key[0] == channel_id:
                del self.history[key]

    def sample(self, channel: dict, states: dict, now: float) -> dict:
        values = {}
        times = []
        statuses = []
        marker = None
        uptime = None
        if channel["mode"] == "counter":
            continuity = states.get(channel["discontinuity_entity"])
            if continuity and self.fresh(continuity, now):
                marker = numeric(continuity["state"])
            boot = states.get(channel["uptime_entity"])
            if boot and self.fresh(boot, now):
                uptime = numeric(boot["state"])
        for direction in ("rx", "tx"):
            key = (channel["id"], direction)
            state = states.get(channel[f"{direction}_entity"])
            value, status = None, "unavailable"
            if state and self.fresh(state, now):
                attribute = channel.get(f"{direction}_attribute", "")
                raw = (
                    state.get("attributes", {}).get(attribute)
                    if attribute
                    else state["state"]
                )
                current = numeric(raw)
                source_unit = state.get("attributes", {}).get("unit_of_measurement")
                if not attribute and source_unit and source_unit != channel["unit"]:
                    same_rate = channel["mode"] == "rate" and RATE_UNITS.get(
                        source_unit
                    ) == RATE_UNITS.get(channel["unit"])
                    if not same_rate:
                        current = None
                stamp = state["timestamp"]
                times.append(stamp)
                if current is not None:
                    if channel["mode"] == "rate":
                        value = float(current * RATE_UNITS[channel["unit"]])
                        status = "available"
                    elif (
                        marker is not None
                        and uptime is not None
                        and not isinstance(raw, float)
                        and current == current.to_integral_value()
                    ):
                        previous = self.history.get(key)
                        status = "warming_up"
                        if previous and marker == previous[2] and uptime >= previous[5]:
                            elapsed = stamp - previous[1]
                            if elapsed == 0 and current == previous[0]:
                                value, status = previous[3], previous[4]
                            elif 0 < elapsed <= MAX_AGE and current >= previous[0]:
                                value = float(
                                    (current - previous[0]) * 8 / Decimal(str(elapsed))
                                )
                                status = "available"
                        self.history[key] = (
                            current,
                            stamp,
                            marker,
                            value,
                            status,
                            uptime,
                        )
            elif state:
                status = (
                    "stale"
                    if state.get("state") not in ("unknown", "unavailable", "not_home")
                    else "unavailable"
                )
            if status in ("unavailable", "stale"):
                self.history.pop(key, None)
            values[f"{direction}BitsPerSecond"] = value
            statuses.append(status)
        status = (
            "available"
            if all(s == "available" for s in statuses)
            else "warming_up"
            if "warming_up" in statuses
            else "stale"
            if "stale" in statuses
            else "unavailable"
        )
        return {
            "id": channel["id"],
            "name": channel["name"],
            "provider": channel["provider"],
            "scope": channel["scope"],
            "deviceId": channel.get("device_id", ""),
            "mode": channel["mode"],
            "status": status,
            "sampleTime": min(times) if times else None,
            **values,
        }

    @staticmethod
    def fresh(state: dict, now: float) -> bool:
        return (
            state.get("state") not in ("unknown", "unavailable", "not_home")
            and isinstance(state.get("timestamp"), (float, int))
            and -5 <= now - state["timestamp"] <= MAX_AGE
        )
