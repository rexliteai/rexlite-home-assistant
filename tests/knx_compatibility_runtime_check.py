"""Run official HA KNX checks with a generated, non-customer ETS fixture.

The fixture contains one switched light and two environmental sensors. It has
no bus devices, credentials or installation data and never sends telegrams.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).parents[1]
NAMESPACE = "http://knx.org/xml/project/20"
PROJECT = "P-0001"


def make_project(path: Path) -> None:
    """Build a deterministic, minimal ETS5 archive from synthetic test data."""
    ET.register_namespace("", NAMESPACE)

    def element(parent, tag, **attributes):
        return ET.SubElement(parent, f"{{{NAMESPACE}}}{tag}", attributes)

    master = ET.Element(f"{{{NAMESPACE}}}KNX")
    element(element(master, "MasterData"), "Manufacturers")
    metadata = ET.Element(
        f"{{{NAMESPACE}}}KNX",
        {"CreatedBy": "REXLiTE compatibility test", "ToolVersion": "5.7.0.0"},
    )
    project = element(metadata, "Project", Id=PROJECT)
    element(
        project,
        "ProjectInformation",
        Name="KNX Compatibility Fixture",
        GroupAddressStyle="ThreeLevel",
        Guid="00000000-0000-4000-8000-000000000001",
        LastModified="2026-01-01T00:00:00",
        ProjectStart="2026-01-01T00:00:00",
    )
    content = ET.Element(f"{{{NAMESPACE}}}KNX")
    project = element(content, "Project", Id=PROJECT)
    installation = element(
        element(project, "Installations"),
        "Installation",
        Name="Synthetic test installation",
        InstallationId="0",
        DefaultLine=f"{PROJECT}-0_L-1",
    )
    area = element(
        element(installation, "Topology"),
        "Area",
        Id=f"{PROJECT}-0_A-1",
        Address="1",
        Name="Test area",
    )
    line = element(
        area,
        "Line",
        Id=f"{PROJECT}-0_L-1",
        Address="1",
        Name="Test line",
        MediumTypeRefId="MT-0",
    )
    element(line, "Segment", Id=f"{PROJECT}-0_S-1", Number="0", MediumTypeRefId="MT-0")
    ranges = element(element(installation, "GroupAddresses"), "GroupRanges")
    main = element(
        ranges,
        "GroupRange",
        Id=f"{PROJECT}-0_GR-1",
        RangeStart="24576",
        RangeEnd="26623",
        Name="Test",
    )
    middle = element(
        main,
        "GroupRange",
        Id=f"{PROJECT}-0_GR-2",
        RangeStart="24576",
        RangeEnd="24831",
        Name="Test entities",
    )
    for index, (name, dpt) in enumerate(
        (
            ("測試燈 開關", "DPST-1-1"),
            ("測試燈 狀態", "DPST-1-11"),
            ("測試溫度", "DPST-9-1"),
            ("測試濕度", "DPST-9-7"),
        ),
        1,
    ):
        element(
            middle,
            "GroupAddress",
            Id=f"{PROJECT}-0_GA-{index}",
            Address=str(24576 + index),
            Name=name,
            DatapointType=dpt,
            Description="Synthetic compatibility fixture",
            Puid=str(index),
        )
    element(installation, "Locations")
    files = {
        "knx_master.xml": ET.tostring(master, encoding="utf-8", xml_declaration=True),
        f"{PROJECT}.signature": b"synthetic-fixture\n",
        f"{PROJECT}/project.xml": ET.tostring(
            metadata, encoding="utf-8", xml_declaration=True
        ),
        f"{PROJECT}/0.xml": ET.tostring(
            content, encoding="utf-8", xml_declaration=True
        ),
    }
    with ZipFile(path, "w") as archive:
        for name, data in files.items():
            info = ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, data)


def main() -> None:
    # Set before importing HA, whose optional imports can include LiteLLM.
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    from xknxproject import XKNXProj

    source = ROOT / "custom_components/rexlite/knx_project_mapper.py"
    spec = importlib.util.spec_from_file_location(
        "rexlite_compatibility_mapper", source
    )
    assert spec is not None and spec.loader is not None
    mapper = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mapper
    spec.loader.exec_module(mapper)
    with tempfile.TemporaryDirectory() as directory:
        fixture = Path(directory) / "compatibility.knxproj"
        make_project(fixture)
        plan = mapper.plan_project(XKNXProj(fixture, language="en-US").parse())
        assert plan["entityCount"] == 3, plan
        assert not plan["skipped"], plan["skipped"]
        assert set(plan["config"]) == {"light", "sensor"}, plan["config"]
        for script, arguments in (
            ("knx_websocket_runtime_check.py", []),
            ("knx_package_runtime_check.py", []),
            ("knx_identity_runtime_check.py", []),
            ("knx_project_import_runtime_check.py", [str(fixture)]),
            ("knx_deployment_runtime_check.py", []),
        ):
            subprocess.run(
                [sys.executable, str(ROOT / "tests" / script), *arguments],
                cwd=ROOT,
                check=True,
            )
    print("Synthetic ETS fixture and all real HA KNX compatibility checks: PASS")


if __name__ == "__main__":
    main()
