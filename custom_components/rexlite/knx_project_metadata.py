"""Recover explicit scene numbers omitted by the standard ETS parser.

Only versioned, verified sender application layouts are supported. A number is
bound to a single-address scene object in the same module instance, never to a
group-address label. Unknown/encrypted layouts retain the original parser data.
"""

from __future__ import annotations

import re
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_MEMBERS = 4096
MAX_XML_BYTES = 8 * 1024 * 1024
MAX_TOTAL_XML_BYTES = 32 * 1024 * 1024

# Application revision: module, scene object, parameter reference, meaning.
# Extending this table requires an ETS fixture proving the binding and encoding.
SCENE_LAYOUTS = {
    "M-0085_A-00BD-11-006E": ("MD-4", "O-2-0_R-56", "UP-59_R-62", "8 bit scene number"),
    "M-02F0_A-0098-10-B2ED-O0085": (
        "MD-5",
        "O-2-2_R-116",
        "UP-192_R-258",
        "Scene number [1..64]",
    ),
    "M-02F0_A-0092-10-CF6C-O0085": (
        "MD-9",
        "O-2-0_R-1",
        "P-28_R-28",
        "Scene number [1..64]",
    ),
}


class _Archive:
    def __init__(self, archive: ZipFile) -> None:
        self.archive = archive
        members = archive.infolist()
        self.members = {entry.filename: entry for entry in members}
        if len(members) > MAX_MEMBERS or len(self.members) != len(members):
            raise ValueError("Unsupported or ambiguous archive")
        self.total = 0

    def xml(self, name: str) -> ET.Element:
        entry = self.members[name]
        if entry.flag_bits & 1 or entry.file_size > MAX_XML_BYTES:
            raise ValueError("Protected or oversized XML")
        self.total += entry.file_size
        if self.total > MAX_TOTAL_XML_BYTES:
            raise ValueError("XML budget exceeded")
        with self.archive.open(entry) as stream:
            data = stream.read(MAX_XML_BYTES + 1)
        if len(data) != entry.file_size or len(data) > MAX_XML_BYTES:
            raise ValueError("Invalid XML size")
        # ETS UTF-8 XML needs neither DTDs nor entity declarations. Reject both
        # before parsing, including UTF-16 input rather than guessing encodings.
        text = data.decode("utf-8-sig")
        if "\x00" in text or "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
            raise ValueError("Unsupported XML declaration")
        return ET.fromstring(text)


def _unique_index(elements: list[ET.Element], attribute: str) -> dict:
    result = {}
    for element in elements:
        key = element.get(attribute)
        if not key or key in result:
            raise ValueError("Ambiguous ETS identifier")
        result[key] = element
    return result


def _devices(root: ET.Element) -> dict[str, ET.Element]:
    result = {}
    for area in root.findall(".//{*}Area"):
        for line in area.findall(".//{*}Line"):
            for device in line.findall(".//{*}DeviceInstance"):
                address = ".".join(
                    str(item.get("Address")) for item in (area, line, device)
                )
                if address in result:
                    raise ValueError("Ambiguous device address")
                result[address] = device
    return result


def _parameter(root: ET.Element, application: str) -> tuple[str, str]:
    module, _, reference, meaning = SCENE_LAYOUTS[application]
    ids = _unique_index([el for el in root.iter() if el.get("Id")], "Id")
    ref_id = f"{application}_{module}_{reference}"
    ref = ids[ref_id]
    parameter = ids[ref.attrib["RefId"]]
    if (
        ref.tag.rsplit("}", 1)[-1] != "ParameterRef"
        or parameter.tag.rsplit("}", 1)[-1] != "Parameter"
        or parameter.get("Id") != ref_id.rsplit("_R-", 1)[0]
        or parameter.get("Name", "").strip() != meaning
    ):
        raise ValueError("Unknown scene parameter meaning")
    parameter_type = ids[parameter.attrib["ParameterType"]]
    number = parameter_type.find("./{*}TypeNumber")
    restriction = parameter_type.find("./{*}TypeRestriction")
    enumeration = parameter_type.findall("./{*}TypeRestriction/{*}Enumeration")
    numeric_range = number is not None and all(
        number.get(key) == value
        for key, value in {
            "Type": "unsignedInt",
            "SizeInBit": "8",
            "minInclusive": "1",
            "maxInclusive": "64",
        }.items()
    )
    enum_range = (
        restriction is not None
        and restriction.get("Base") == "Value"
        and restriction.get("SizeInBit") == "8"
        and len(enumeration) == 64
        and {el.get("Value") for el in enumeration} == {str(n) for n in range(1, 65)}
    )
    if not numeric_range and not enum_range:
        raise ValueError("Unknown scene-number encoding")
    return ref_id, ref.get("Value", parameter.get("Value", ""))


def _sender_number(
    object_id: str,
    obj: dict,
    ga: dict,
    project: dict,
    devices: dict,
    parameters: dict,
) -> int:
    device_address = obj["device_address"]
    application = project["devices"][device_address]["application"]
    module, object_suffix, reference, _ = SCENE_LAYOUTS[application]
    device_part, local_id = object_id.split("/", 1)
    match = re.fullmatch(
        rf"({module}_M-[0-9]+_MI-[0-9]+)_{re.escape(object_suffix)}", local_id
    )
    if not match or device_part != device_address:
        raise ValueError("Unknown scene object layout")
    raw_device = devices[device_address]
    objects = _unique_index(raw_device.findall(".//{*}ComObjectInstanceRef"), "RefId")
    raw_object = objects[local_id]
    # Corroborate the parsed object against the same raw project's exact link.
    if raw_object.get("Links", "").split() != [ga["identifier"]]:
        raise ValueError("Scene sender has ambiguous links")
    refs = _unique_index(raw_device.findall(".//{*}ParameterInstanceRef"), "RefId")
    _, default = parameters[application]
    instance = refs.get(f"{application}_{match.group(1)}_{reference}")
    value = instance.get("Value", "") if instance is not None else default
    if not re.fullmatch(r"[0-9]+", value) or not 1 <= int(value) <= 64:
        raise ValueError("Invalid scene number")
    return int(value)


def enrich_project(
    project: dict, path: str | Path, password: str | None = None
) -> dict:
    """Return a shallow copy with proven scene annotations; never alter input.

    This optional enrichment intentionally skips protected project archives.
    The upstream parser still handles their password and imports them normally.
    """
    del password  # No password is needed or retained by this unencrypted reader.
    try:
        project_id = project["info"]["project_id"]
        if not re.fullmatch(r"P-[A-Fa-f0-9]+", project_id):
            return project
        if Path(path).stat().st_size > MAX_ARCHIVE_BYTES:
            return project
        with ZipFile(path) as archive:
            bounded = _Archive(archive)
            devices = _devices(bounded.xml(f"{project_id}/0.xml"))
            applications = {
                value.get("application") for value in project["devices"].values()
            } & SCENE_LAYOUTS.keys()
            parameters = {}
            for application in sorted(applications):
                root = bounded.xml(f"{application[:6]}/{application}.xml")
                parameters[application] = _parameter(root, application)
        groups = dict(project["group_addresses"])
        objects = project["communication_objects"]
        for address, ga in sorted(groups.items()):
            senders, numbers = [], set()
            try:
                for object_id in ga.get("communication_object_ids", []):
                    obj = objects[object_id]
                    flags = obj.get("flags", {})
                    if not (
                        flags.get("communication") is True
                        and flags.get("transmit") is True
                    ):
                        continue
                    if (
                        obj.get("group_address_links") != [address]
                        or not obj.get("dpts")
                        or any(
                            d not in ({"main": 17, "sub": 1}, {"main": 18, "sub": 1})
                            for d in obj["dpts"]
                        )
                    ):
                        raise ValueError("Sender is not an unambiguous scene object")
                    numbers.add(
                        _sender_number(object_id, obj, ga, project, devices, parameters)
                    )
                    senders.append(object_id)
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
            if senders and len(numbers) == 1:
                groups[address] = {
                    **ga,
                    "scene_number": next(iter(numbers)),
                    "scene_number_source": "ets-sender-parameter",
                    "scene_sender_ids": sorted(senders),
                }
        return {**project, "group_addresses": groups}
    except (
        OSError,
        BadZipFile,
        KeyError,
        TypeError,
        ValueError,
        AttributeError,
        RuntimeError,
        NotImplementedError,
        ET.ParseError,
    ):
        return project
