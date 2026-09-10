"""Deterministic ETS metadata to Home Assistant KNX YAML planning.

This module never sends telegrams or writes files. Addresses are joined only by
ETS Function roles or exact, shared conventional name prefixes. Device names,
address offsets and a DPT alone never establish a relationship between channels.
Scene numbers must be explicit structured metadata; DPT 17/18 does not contain
that information. Unsupported addresses remain visible in the plan.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from typing import Any

MAX_GROUP_ADDRESSES = 65535
MAX_ENTITIES = 2000
MAPPER_REVISION = 3

# role: (YAML field, allowed exact DPTs). Names here are semantic roles, not GA
# names. Standard ETS roles include AbsoluteSetvalueControl/ActualDimmingValue.
ROLE_FIELDS = {
    "switchonoff": ("address", {(1, 1)}),
    "infoonoff": ("state_address", {(1, 1), (1, 11)}),
    "statusonoff": ("state_address", {(1, 1), (1, 11)}),
    "absolutesetvaluecontrol": ("brightness_address", {(5, 1)}),
    "dimmingvalue": ("brightness_address", {(5, 1)}),
    "brightnessvalue": ("brightness_address", {(5, 1)}),
    "actualdimmingvalue": ("brightness_state_address", {(5, 1)}),
    "infodimmingvalue": ("brightness_state_address", {(5, 1)}),
    "moveupdown": ("move_long_address", {(1, 8)}),
    "stopstepupdown": ("move_short_address", {(1, 7)}),
    "stop": ("stop_address", {(1, 10)}),
    "position": ("position_address", {(5, 1)}),
    "positionvalue": ("position_address", {(5, 1)}),
    "currentabsolutepositionblindspercentage": ("position_state_address", {(5, 1)}),
    "positionstate": ("position_state_address", {(5, 1)}),
    "angle": ("angle_address", {(5, 1)}),
    "currentabsolutepositionslatpercentage": ("angle_state_address", {(5, 1)}),
    "temproom": ("temperature_address", {(9, 1)}),
    "temperature": ("temperature_address", {(9, 1)}),
    "targettemperature": ("target_temperature_address", {(9, 1)}),
    "targettemperaturestate": ("target_temperature_state_address", {(9, 1)}),
    "controllermode": ("controller_mode_address", {(20, 105)}),
    "controllermodestate": ("controller_mode_state_address", {(20, 105)}),
    "operationmode": ("operation_mode_address", {(20, 102)}),
    "operationmodestate": ("operation_mode_state_address", {(20, 102)}),
    "fanspeed": ("fan_speed_address", {(5, 1)}),
    "fanspeedstate": ("fan_speed_state_address", {(5, 1)}),
    "scene": ("address", {(17, 1), (18, 1)}),
}

# Longest suffix wins. A separator and a non-empty exact prefix are required.
# This supports existing installer naming without combining vaguely similar
# labels or inferring a feedback address by incrementing a group address.
# The shade roles "close"/"step"/"pos" are single-address labels for the
# bi-directional up/down (DPT 1.008), step/stop (DPT 1.007) and absolute
# position (DPT 5.001) group addresses; the exact-DPT guard below still rejects
# a label that does not carry the datapoint type its role requires. "open" is
# deliberately absent: paired with "close" it only produces an ambiguous
# duplicate up/down role.
NAME_ROLES = {
    "亮度狀態": "actualdimmingvalue",
    "brightness status": "actualdimmingvalue",
    "亮度": "absolutesetvaluecontrol",
    "brightness": "absolutesetvaluecontrol",
    "開關": "switchonoff",
    "on/off": "switchonoff",
    "switch": "switchonoff",
    "狀態": "infoonoff",
    "status": "infoonoff",
    "上下": "moveupdown",
    "up/down": "moveupdown",
    "close": "moveupdown",
    "停止/微調": "stopstepupdown",
    "stop/step": "stopstepupdown",
    "step": "stopstepupdown",
    "停止": "stop",
    "stop": "stop",
    "位置狀態": "positionstate",
    "position status": "positionstate",
    "位置": "position",
    "position": "position",
    "pos": "position",
    "目標溫度狀態": "targettemperaturestate",
    "設定溫度狀態": "targettemperaturestate",
    "target temperature status": "targettemperaturestate",
    "目標溫度": "targettemperature",
    "設定溫度": "targettemperature",
    "target temperature": "targettemperature",
    "室內溫度": "temproom",
    "室溫": "temproom",
    "溫度": "temproom",
    "temperature": "temproom",
    "模式狀態": "controllermodestate",
    "模式": "controllermode",
    "風速狀態": "fanspeedstate",
    "風速": "fanspeed",
}

# Measurement-only fallbacks never expose a write action. Other exact DPTs can
# still be represented when a communication object proves this is telemetry.
MEASUREMENT_DPTS = {
    (9, sub)
    for sub in (1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29)
}
TELEMETRY_DPTS = MEASUREMENT_DPTS | {
    (5, 1),
    (5, 3),
    (5, 4),
    (5, 5),
    (5, 6),
    (5, 10),
    (7, 1),
    (7, 2),
    (7, 3),
    (7, 4),
    (7, 5),
    (7, 6),
    (7, 600),
    (12, 1),
    (13, 1),
    (13, 10),
    (13, 13),
    (14, 56),
    (14, 76),
}
LIGHT_TYPES = {"switchablelight", "dimmablelight", "light", "ft1", "ft6"}
COVER_TYPES = {"sunprotection", "cover", "blinds", "ft7"}
CLIMATE_TYPES = {"heatingradiator", "heatingfloor", "climate", "hvac", "ft8"}

# Installer abbreviations require communication-object channel evidence below;
# they are deliberately not added to the name-only convention table.
ABBREVIATED_ROLES = {
    "SW": ("address", {(1, 1)}),
    "SW-FB": ("state_address", {(1, 1), (1, 11)}),
    "VAL": ("brightness_address", {(5, 1)}),
    "VAL-FB": ("brightness_state_address", {(5, 1)}),
    "CT": ("color_temperature_address", {(7, 600)}),
    "CT-FB": ("color_temperature_state_address", {(7, 600)}),
}


def _semantic(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def _address(value: Any) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+(?:/[0-9]+){0,2}", value):
        return None
    parts = [int(part) for part in value.split("/")]
    limits = {1: [65535], 2: [31, 2047], 3: [31, 7, 255]}[len(parts)]
    if any(part > limit for part, limit in zip(parts, limits, strict=True)):
        return None
    raw = parts[0] if len(parts) == 1 else parts[0] * 2048 + parts[1]
    if len(parts) == 3:
        raw = parts[0] * 2048 + parts[1] * 256 + parts[2]
    return f"{raw // 2048}/{(raw % 2048) // 256}/{raw % 256}"


def _dpt(value: Any) -> tuple[int, int | None] | None:
    if not isinstance(value, dict):
        return None
    main, sub = value.get("main"), value.get("sub")
    if type(main) is not int or main <= 0:
        return None
    if sub is not None and (type(sub) is not int or sub < 0):
        return None
    return main, sub


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _label(value: Any, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    return " ".join(value.split())[:120] or fallback


def _named_role(name: str) -> tuple[str, str] | None:
    for suffix in sorted(NAME_ROLES, key=len, reverse=True):
        match = re.fullmatch(r"(.+?)[\s_-]+" + re.escape(suffix), name, re.IGNORECASE)
        if match and (prefix := match.group(1).strip()):
            return prefix, NAME_ROLES[suffix]
    return None


class _Planner:
    def __init__(self, project: dict) -> None:
        self.project = project
        info = _dict(project.get("info"))
        self.project_id = _label(info.get("guid") or info.get("project_id"), "")
        if not self.project_id:
            # The writer owns one managed project per HA host. Without an ETS
            # project identifier, keep this host-local namespace stable when
            # addresses are added or renamed on the next import.
            self.project_id = "host-managed-project"
        self.groups: dict[str, dict] = {}
        self.aliases: dict[str, str] = {}
        self.objects: dict[str, list[dict]] = defaultdict(list)
        self.dpts: dict[str, tuple[int, int]] = {}
        self.dpt_choices: dict[str, set[tuple[int, int | None] | None]] = {}
        self.issues: dict[str, str] = {}
        self.blocked: set[str] = set()
        self.used: set[str] = set()
        self.candidates: list[dict] = []
        self._load()

    def _load(self) -> None:
        source = _dict(self.project.get("group_addresses"))
        if len(source) > MAX_GROUP_ADDRESSES:
            raise ValueError("ETS project exceeds the supported group-address limit")
        for key in sorted(source):
            ga = _dict(source[key])
            address = _address(ga.get("address") or str(key))
            if address is None or address == "0/0/0":
                self.issues[str(key)] = "invalid_group_address"
                continue
            if address in self.groups and self.groups[address] != ga:
                self.issues[address] = "duplicate_group_address_metadata"
                self.blocked.add(address)
                continue
            self.groups[address] = ga
            for alias in (str(key), address, ga.get("identifier")):
                if isinstance(alias, str) and alias:
                    self.aliases[alias] = address
        objects = _dict(self.project.get("communication_objects"))
        linked_ids: dict[str, set[str]] = defaultdict(set)
        for object_id, raw in objects.items():
            for link in _list(_dict(raw).get("group_address_links")):
                address = self.aliases.get(str(link), _address(link))
                if address in self.groups:
                    linked_ids[address].add(object_id)
        for address, ga in self.groups.items():
            object_ids = {
                item
                for item in _list(ga.get("communication_object_ids"))
                if isinstance(item, str)
            } | linked_ids[address]
            linked = [
                {**_dict(objects[item]), "_object_id": item}
                for item in sorted(object_ids)
                if item in objects
            ]
            self.objects[address] = linked
            candidates = []
            if ga.get("dpt") is not None:
                candidates.append(_dpt(ga["dpt"]))
            for obj in linked:
                candidates.extend(_dpt(dpt) for dpt in _list(obj.get("dpts")))
            exact = {dpt for dpt in candidates if dpt and dpt[1] is not None}
            mains = {dpt[0] for dpt in candidates if dpt}
            self.dpt_choices[address] = set(candidates)
            # An object's list declares supported alternatives, not simultaneous
            # wire encodings. An explicit GA subtype can select one only when
            # every declared object accepts that exact type (or its main type).
            declared = _dpt(ga.get("dpt"))
            if (
                declared
                and declared[1] is not None
                and all(
                    not (options := _list(obj.get("dpts")))
                    or (
                        all(_dpt(option) is not None for option in options)
                        and any(
                            _dpt(option) in {declared, (declared[0], None)}
                            for option in options
                        )
                    )
                    for obj in linked
                )
            ):
                exact, mains = {declared}, {declared[0]}
            if None in candidates or len(exact) != 1 or len(mains) != 1:
                self.issues[address] = (
                    "conflicting_datapoint_type"
                    if len(exact) > 1 or len(mains) > 1
                    else "missing_exact_datapoint_type"
                )
                self.blocked.add(address)
            else:
                self.dpts[address] = next(iter(exact))

    def has_flag(self, address: str, flag: str) -> bool:
        return any(
            _dict(obj.get("flags")).get("communication") is True
            and _dict(obj.get("flags")).get(flag) is True
            for obj in self.objects[address]
        )

    @staticmethod
    def _channel_key(obj: dict) -> tuple[str, ...] | None:
        """Identify a channel without confusing multi-instance panel modules."""
        device = obj.get("device_address")
        if not isinstance(device, str) or not device:
            return None
        object_id = str(obj.get("_object_id", ""))
        module = re.search(r"/(MD-[^/]+)_O-", object_id)
        channel = str(obj.get("channel") or "")
        if module:
            return device, "module", module.group(1), channel
        text = _label(obj.get("text"), "")
        if text:
            return device, "channel", channel, text
        return None

    def _actuator_keys(self, address: str, *, feedback: bool) -> set[tuple]:
        keys = set()
        for obj in self.objects[address]:
            flags = _dict(obj.get("flags"))
            if flags.get("communication") is not True:
                continue
            if feedback:
                # A receiver/display may also be writable on a feedback GA.
                # Use the producing actuator's flags, not the union of flags.
                proven = (
                    flags.get("write") is False
                    and flags.get("read") is True
                    and flags.get("transmit") is True
                )
            else:
                proven = flags.get("write") is True and flags.get("transmit") is False
            if proven and (key := self._channel_key(obj)):
                keys.add(key)
        return keys

    def abbreviated_groups(self) -> None:
        """Join proven actuator channels with exact SW/VAL/CT installer roles."""
        grouped: dict[str, dict[str, list[str]]] = defaultdict(
            lambda: defaultdict(list)
        )
        names = {}
        for address, ga in sorted(self.groups.items()):
            if address in self.used:
                continue
            match = re.fullmatch(
                r"(.+?)[\s_-]+(SW-FB|VAL-FB|CT-FB|SW|VAL|CT)",
                _label(ga.get("name"), ""),
                re.IGNORECASE,
            )
            if match:
                name, role = match.groups()
                key = name.casefold()
                names[key] = name
                grouped[key][role.upper()].append(address)
        for key, roles in sorted(grouped.items()):
            commands = roles.get("SW", [])
            if len(commands) != 1:
                # Never choose between duplicate command roles.
                if len(commands) > 1:
                    self.blocked.update(commands)
                    for address in commands:
                        self.issues[address] = "ambiguous_duplicate_function_role"
                continue
            command = commands[0]
            if command in self.blocked or self.dpts.get(command) != (1, 1):
                continue
            anchor = self._actuator_keys(command, feedback=False)
            if len(anchor) != 1:
                # A fan-out command may control several independent outputs;
                # one output's feedback cannot prove the aggregate state.
                continue
            fields = {"address": command}
            is_light = bool(re.search(r"燈|照明|\blight\b", names[key], re.IGNORECASE))
            for role, addresses in sorted(roles.items()):
                if role == "SW" or (not is_light and role != "SW-FB"):
                    continue
                field, expected = ABBREVIATED_ROLES[role]
                if len(addresses) != 1:
                    for address in addresses:
                        self.blocked.add(address)
                        self.issues[address] = "ambiguous_duplicate_function_role"
                    continue
                address = addresses[0]
                if anchor != self._actuator_keys(
                    address, feedback=role.endswith("-FB")
                ):
                    self.issues.setdefault(address, "unproven_same_actuator_channel")
                    continue
                # DPT Switch and Status share 0=off/1=on. Permit only this
                # exact feedback disagreement after proving the same producer.
                if (
                    role == "SW-FB"
                    and self.issues.get(address) == "conflicting_datapoint_type"
                    and self.dpt_choices[address] == {(1, 1), (1, 11)}
                ):
                    self.dpts[address] = (1, 11)
                    self.blocked.discard(address)
                    self.issues.pop(address, None)
                if address in self.blocked or self.dpts.get(address) not in expected:
                    self.issues.setdefault(address, "role_datapoint_type_mismatch")
                    continue
                fields[field] = address
            # Brightness / CT features are independent: a malformed optional
            # feature must not erase a proven on/off channel. Orphan feedback
            # remains eligible for an observation-only fallback.
            for feature in ("brightness", "color_temperature"):
                if f"{feature}_address" not in fields:
                    fields.pop(f"{feature}_state_address", None)
            if "color_temperature_address" in fields:
                fields["color_temperature_mode"] = "absolute"
            if len(fields) == 1:
                # Retain the existing single-address fallback when pairing
                # proves no additional feature. Never fabricate state feedback.
                continue
            self._add(
                "light" if is_light else "switch",
                names[key],
                fields,
                "exact-name-object-channel",
            )

    def scene_metadata(self) -> None:
        """Use explicit sender parameters recovered from the same ETS archive."""
        for address, ga in sorted(self.groups.items()):
            if address in self.used:
                continue
            number = ga.get("scene_number")
            sender_ids = ga.get("scene_sender_ids")
            if (
                ga.get("scene_number_source") != "ets-sender-parameter"
                or type(number) is not int
                or not 1 <= number <= 64
                or not isinstance(sender_ids, list)
                or not sender_ids
                or any(not isinstance(item, str) or not item for item in sender_ids)
            ):
                continue
            linked = {obj["_object_id"]: obj for obj in self.objects[address]}
            if not all(
                sender in linked
                and _dict(linked[sender].get("flags")).get("communication") is True
                and _dict(linked[sender].get("flags")).get("transmit") is True
                for sender in sender_ids
            ) or not self.has_flag(address, "write"):
                continue
            choices = self.dpt_choices[address]
            if not choices or not choices <= {(17, 1), (18, 1)}:
                continue
            if address in self.blocked:
                if self.issues.get(address) != "conflicting_datapoint_type":
                    continue
                # Both scene encodings share the lower six recall bits. This
                # path only recalls an explicit 1..64 scene and never sets the
                # learn/store bit; it does not generalize DPT compatibility.
                self.blocked.discard(address)
                self.issues.pop(address, None)
            self._add(
                "scene",
                _label(ga.get("name"), f"KNX Scene {number}"),
                {"address": address, "scene_number": number},
                "ets-sender-scene-parameter",
            )

    def _add(self, platform: str, name: str, config: dict, source: str) -> None:
        addresses = sorted({v for k, v in config.items() if k.endswith("address")})
        primary = (
            config.get("address")
            or config.get("move_long_address")
            or config.get("position_address")
            or config.get("target_temperature_address")
            or config.get("state_address")
            or config.get("temperature_address")
        )
        identity = f"{self.project_id}|{platform}|{primary}"
        if platform == "scene":
            identity += f"|{config['scene_number']}"
        unique_id = "rexlite_ets_" + hashlib.sha256(identity.encode()).hexdigest()[:24]
        self.candidates.append(
            {
                "platform": platform,
                "name": _label(name, f"KNX {primary}"),
                "uniqueId": unique_id,
                "addresses": addresses,
                "source": source,
                "primaryAddress": primary,
                "config": config,
            }
        )
        self.used.update(addresses)

    def _group(
        self,
        name: str,
        kind: str,
        refs: list[tuple[str, str]],
        source: str,
        metadata: dict | None = None,
    ) -> None:
        fields: dict[str, str] = {}
        mentioned = {address for _, address in refs if address in self.groups}
        error = None
        for role, address in refs:
            if role not in ROLE_FIELDS:
                continue
            field, expected = ROLE_FIELDS[role]
            if address not in self.groups or address in self.blocked:
                error = "invalid_or_missing_function_address"
                break
            if self.dpts[address] not in expected:
                error = "role_datapoint_type_mismatch"
                break
            if field in fields and fields[field] != address:
                error = "ambiguous_duplicate_function_role"
                break
            # KNX object's Write flag means the actuator accepts group writes.
            # Explicit metadata may work without objects, but must never
            # contradict present, complete object flags.
            if (
                field not in {"state_address", "temperature_address"}
                and not field.endswith("state_address")
                and self.objects[address]
                and not self.has_flag(address, "write")
            ):
                error = "command_address_not_writable"
                break
            fields[field] = address
        if error:
            self.blocked.update(mentioned)
            for address in mentioned:
                self.issues.setdefault(address, error)
            return
        platform = None
        kind = _semantic(kind)
        if kind in CLIMATE_TYPES or "target_temperature_address" in fields:
            required = {
                "temperature_address",
                "target_temperature_address",
                "target_temperature_state_address",
            }
            if required <= fields.keys():
                platform = "climate"
                if "address" in fields:
                    fields["on_off_address"] = fields.pop("address")
                if "state_address" in fields:
                    fields["on_off_state_address"] = fields.pop("state_address")
            elif any(k in fields for k in required - {"temperature_address"}):
                for address in mentioned:
                    self.issues.setdefault(address, "incomplete_climate_addresses")
                self.blocked.update(mentioned)
                return
        elif kind in COVER_TYPES or "move_long_address" in fields:
            if "move_long_address" in fields or "position_address" in fields:
                platform = "cover"
        elif kind in {"scene", "scenes"}:
            scene_number = _dict(metadata).get("scene_number")
            if (
                "address" in fields
                and type(scene_number) is int
                and 1 <= scene_number <= 64
            ):
                platform = "scene"
                fields["scene_number"] = scene_number
            else:
                for address in mentioned:
                    self.issues.setdefault(address, "missing_explicit_scene_number")
                self.blocked.update(mentioned)
                return
        elif "address" in fields:
            platform = "light" if kind in LIGHT_TYPES else "switch"
            if "brightness_address" in fields and platform != "light":
                platform = None
        if platform is None:
            return
        allowed = {
            "light": {
                "address",
                "state_address",
                "brightness_address",
                "brightness_state_address",
            },
            "switch": {"address", "state_address"},
            "cover": {
                "move_long_address",
                "move_short_address",
                "stop_address",
                "position_address",
                "position_state_address",
                "angle_address",
                "angle_state_address",
            },
            "scene": {"address", "scene_number"},
            "climate": {
                "temperature_address",
                "target_temperature_address",
                "target_temperature_state_address",
                "on_off_address",
                "on_off_state_address",
                "controller_mode_address",
                "controller_mode_state_address",
                "operation_mode_address",
                "operation_mode_state_address",
                "fan_speed_address",
                "fan_speed_state_address",
            },
        }.get(platform)
        if allowed is not None and not fields.keys() <= allowed:
            self.blocked.update(mentioned)
            for address in mentioned:
                self.issues.setdefault(address, "incompatible_function_roles")
            return
        self._add(platform, name, fields, source)

    def functions(self) -> None:
        for function_id, raw in sorted(_dict(self.project.get("functions")).items()):
            function = _dict(raw)
            refs = []
            for ref_key, raw_ref in _dict(function.get("group_addresses")).items():
                ref = _dict(raw_ref)
                raw_address = ref.get("address")
                address = self.aliases.get(str(raw_address), _address(raw_address))
                role = _semantic(ref.get("role") or ref_key)
                if address is None:
                    address = str(raw_address or ref_key)
                    self.issues[address] = "missing_function_address"
                # TempRoomSetpoint is not inherently a write or state role. Use
                # both only when the object flags explicitly prove both roles.
                if role == "temproomsetpoint":
                    if self.has_flag(address, "write") and self.has_flag(
                        address, "read"
                    ):
                        refs.extend(
                            [
                                ("targettemperature", address),
                                ("targettemperaturestate", address),
                            ]
                        )
                    continue
                refs.append((role, address))
            self._group(
                _label(function.get("name"), function_id),
                function.get("function_type", ""),
                refs,
                "ets-function-role",
                function,
            )

    def named_groups(self) -> None:
        grouped: dict[str, list[tuple[str, str]]] = defaultdict(list)
        names = {}
        for address, ga in sorted(self.groups.items()):
            if address in self.used:
                continue
            parsed = _named_role(_label(ga.get("name"), ""))
            if parsed:
                name, role = parsed
                key = name.casefold()
                names[key] = name
                grouped[key].append((role, address))
        for key, refs in sorted(grouped.items()):
            name = names[key]
            kind = (
                "light"
                if re.search(r"燈|照明|\blight\b", name, re.IGNORECASE)
                else "switch"
            )
            self._group(name, kind, refs, "exact-name-role")

    def fallback(self) -> None:
        for address, ga in sorted(self.groups.items()):
            if address in self.used or address in self.blocked:
                continue
            dpt = self.dpts[address]
            name = _label(ga.get("name"), f"KNX {address}")
            named_feedback = bool(re.search(r"[\s_-]FB$", name, re.IGNORECASE))
            telemetry = self.has_flag(address, "transmit") and (
                not self.has_flag(address, "write") or named_feedback
            )
            if dpt in {(17, 1), (18, 1)}:
                self.issues[address] = "missing_explicit_scene_number"
            elif dpt == (1, 11) or (
                dpt in {(1, 1), (1, 2), (1, 5), (1, 9), (1, 18), (1, 19), (1, 22)}
                and telemetry
            ):
                config = {"state_address": address}
                if not self.has_flag(address, "read"):
                    config["sync_state"] = False
                self._add("binary_sensor", name, config, "ets-dpt-telemetry")
            elif (
                dpt == (1, 1) and self.has_flag(address, "write") and not named_feedback
            ):
                self._add("switch", name, {"address": address}, "ets-dpt-object-flags")
            elif dpt in MEASUREMENT_DPTS or (dpt in TELEMETRY_DPTS and telemetry):
                config = {"state_address": address, "type": f"{dpt[0]}.{dpt[1]:03d}"}
                if self.objects[address] and not self.has_flag(address, "read"):
                    config["sync_state"] = False
                self._add("sensor", name, config, "ets-dpt-telemetry")
            else:
                self.issues[address] = "insufficient_supported_entity_metadata"

    def result(self) -> dict:
        # Any disagreement about the same command address is a conflict, not
        # an opportunity to pick whichever record happened to be first.
        claims: dict[str, list[dict]] = defaultdict(list)
        for candidate in self.candidates:
            if self.blocked.intersection(candidate["addresses"]):
                continue
            config = candidate["config"]
            key = candidate["primaryAddress"]
            if candidate["platform"] == "scene":
                key += f"|scene|{config['scene_number']}"
            claims[key].append(candidate)
        accepted = []
        for candidates in claims.values():
            fingerprints = {
                json.dumps((c["platform"], c["config"]), sort_keys=True)
                for c in candidates
            }
            if len(fingerprints) != 1:
                for candidate in candidates:
                    for address in candidate["addresses"]:
                        self.issues[address] = "conflicting_entity_address_claim"
                continue
            accepted.append(candidates[0])
        if len(accepted) > MAX_ENTITIES:
            raise ValueError("ETS project exceeds the supported entity limit")
        config: dict[str, list[dict]] = defaultdict(list)
        entities = []
        used_names: set[tuple[str, str]] = set()
        mapped = set()
        for candidate in sorted(
            accepted, key=lambda c: (c["platform"], c["primaryAddress"], c["uniqueId"])
        ):
            platform, name = candidate["platform"], candidate["name"]
            base = name
            suffix = candidate["primaryAddress"]
            if platform == "scene":
                suffix += f" · {candidate['config']['scene_number']}"
            serial = 1
            while (platform, name.casefold()) in used_names:
                serial_text = f" · {serial}" if serial > 1 else ""
                name = f"{base[:80]} · {suffix}{serial_text}"
                serial += 1
            used_names.add((platform, name.casefold()))
            config[platform].append(
                {
                    "name": name,
                    "unique_id": candidate["uniqueId"],
                    **candidate["config"],
                }
            )
            entities.append(
                {k: v for k, v in candidate.items() if k != "config"} | {"name": name}
            )
            mapped.update(candidate["addresses"])
        skipped = [
            {
                "address": address,
                "reason": self.issues.get(
                    address, "insufficient_supported_entity_metadata"
                ),
            }
            for address in sorted((self.groups.keys() | self.issues.keys()) - mapped)
        ]
        return {
            "projectId": self.project_id,
            "config": dict(config),
            "entityCount": len(entities),
            "entities": entities,
            "skipped": skipped,
            "groupAddressCount": len(self.groups),
            "mappedAddressCount": len(mapped),
        }


def plan_project(project: dict) -> dict:
    """Return a stable, side-effect-free KNX YAML plan and unresolved addresses."""
    if not isinstance(project, dict):
        raise ValueError("Parsed ETS project must be an object")
    planner = _Planner(project)
    planner.functions()
    planner.abbreviated_groups()
    planner.scene_metadata()
    planner.named_groups()
    planner.fallback()
    return planner.result()
