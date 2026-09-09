"""Validate a supplied ETS file using real HA upload, parser and storage APIs."""

import asyncio
import importlib.util
import shutil
import sys
import tempfile
import types
from pathlib import Path

from homeassistant.components.file_upload import FileUploadData
from homeassistant.components.knx.const import KNX_MODULE_KEY
from homeassistant.components.knx.project import KNXProject
from homeassistant.core import HomeAssistant
from xknx import XKNX

BASE = Path(__file__).parents[1] / "custom_components/rexlite"
package = types.ModuleType("rexlite_real_import")
package.__path__ = [str(BASE)]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location(
    package.__name__ + ".knx_project_deployment", BASE / "knx_project_deployment.py"
)
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)


async def main(source: Path) -> None:
    fingerprint = m.digest(await asyncio.to_thread(source.read_bytes))
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "configuration.yaml").write_text("default_config:\n")
        upload = root / "uploads/test-file"
        upload.mkdir(parents=True)
        shutil.copyfile(source, upload / "project.knxproj")
        hass = HomeAssistant(directory)
        entry = types.SimpleNamespace(entry_id="isolated-validation")
        project = KNXProject(hass, entry)
        hass.data[KNX_MODULE_KEY] = types.SimpleNamespace(
            entry=entry, project=project, xknx=XKNX()
        )
        hass.data["file_upload"] = FileUploadData(
            root / "uploads", {"test-file": "project.knxproj"}
        )
        deployer = m.ProjectDeployer(hass)
        result = await deployer.process_project("test-file", "", fingerprint)
        assert result["projectFingerprint"] == fingerprint
        assert project.loaded
        parsed = await project.get_knxproject()
        assert result["parsedProjectDigest"] == m.project_digest(parsed)
        assert deployer.files.read_json(m.IMPORT_ASSOCIATION) == result
        assert not upload.exists()  # Official upload helper removed the temporary copy.
        plan = m.adapt_plan_identity(
            m.plan_project(parsed),
            deployer._compatibility()["identityMode"],
            deployer._identity_format(),
        )
        validated = deployer._schema(plan["config"])
        assert len(plan["entities"]) == plan["entityCount"]
        print("Real HA import, file binding, storage and CONFIG_SCHEMA: PASS")
        print("Mapped entities:", plan["entityCount"], "platforms:", sorted(validated))
        await hass.async_stop(force=True)
    assert m.digest(await asyncio.to_thread(source.read_bytes)) == fingerprint


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
