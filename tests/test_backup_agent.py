"""MAX'Is 雲端 as a Home Assistant backup location, without Home Assistant."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

PACKAGE = Path(__file__).parents[1] / "custom_components/rexlite"


def _module(name: str) -> types.ModuleType:
    """A stub module, shared with the other test files that stub it too."""

    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(name)
        sys.modules[name] = module
    return module


@dataclass
class _AgentBackup:
    addons: list[Any]
    backup_id: str
    date: str
    database_included: bool
    extra_metadata: dict[str, Any]
    folders: list[Any]
    homeassistant_included: bool
    homeassistant_version: str | None
    name: str
    protected: bool
    size: int
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("extra")
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> _AgentBackup:
        return cls(
            addons=list(data["addons"]),
            backup_id=data["backup_id"],
            date=data["date"],
            database_included=data["database_included"],
            extra_metadata=data["extra_metadata"],
            folders=list(data["folders"]),
            homeassistant_included=data["homeassistant_included"],
            homeassistant_version=data["homeassistant_version"],
            name=data["name"],
            protected=data["protected"],
            size=data["size"],
        )


class _BackupAgent:
    domain: str
    unique_id: str

    @property
    def agent_id(self) -> str:
        return f"{self.domain}.{self.unique_id}"


class _BackupAgentError(Exception):
    pass


class _BackupNotFound(_BackupAgentError):
    pass


_module("homeassistant")
_module("homeassistant.components")
backup_component = types.ModuleType("homeassistant.components.backup")
backup_component.AgentBackup = _AgentBackup
backup_component.BackupAgent = _BackupAgent
backup_component.BackupAgentError = _BackupAgentError
backup_component.BackupNotFound = _BackupNotFound
sys.modules["homeassistant.components.backup"] = backup_component
core = _module("homeassistant.core")
if not hasattr(core, "HomeAssistant"):
    core.HomeAssistant = object
if not hasattr(core, "callback"):
    core.callback = lambda function: function
_module("homeassistant.helpers")
aiohttp_client = _module("homeassistant.helpers.aiohttp_client")
if not hasattr(aiohttp_client, "async_get_clientsession"):
    aiohttp_client.async_get_clientsession = lambda hass: object()
ha_const = _module("homeassistant.const")
if not hasattr(ha_const, "Platform"):
    ha_const.Platform = types.SimpleNamespace(
        SENSOR="sensor", BINARY_SENSOR="binary_sensor", SWITCH="switch"
    )

package = types.ModuleType("rexlite_backup_agent_tests")
package.__path__ = [str(PACKAGE)]
package.DATA_BACKUP_AGENT_LISTENERS = "rexlite.backup_agent_listeners"
package.REXLiTEConfigEntry = object
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location(
    f"{package.__name__}.backup", PACKAGE / "backup.py"
)
agent_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = agent_module
spec.loader.exec_module(agent_module)
cloud = sys.modules[f"{package.__name__}.cloud_backup"]

BACKUP = {
    "addons": [],
    "backup_id": "a1b2c3d4",
    "date": "2026-11-13T03:00:00+08:00",
    "database_included": True,
    "extra_metadata": {},
    "folders": [],
    "homeassistant_included": True,
    "homeassistant_version": "2026.8.0",
    "name": "Automatic backup",
    "protected": True,
    "size": 6,
}


class _Entry:
    def __init__(self) -> None:
        self.data = {"agent_id": "hub-1", "agent_auth_token": "token-1"}


class _Hass:
    def __init__(self, loaded: list[_Entry]) -> None:
        self.data: dict[str, Any] = {}
        self.config_entries = types.SimpleNamespace(
            async_loaded_entries=lambda domain: loaded if domain == "rexlite" else []
        )


class _Client:
    """Stands in for MaxisCloudBackupClient."""

    def __init__(self) -> None:
        self.failure: Exception | None = None
        self.stream_failure: Exception | None = None
        self.listed: list[Any] = []
        self.uploaded: list[tuple[dict[str, Any], int, bytes]] = []
        self.deleted: list[str] = []

    def check(self) -> None:
        if self.failure:
            raise self.failure

    async def list_backups(self) -> list[Any]:
        self.check()
        return self.listed

    async def upload(self, backup, size, open_body) -> None:
        self.check()
        body = await open_body()
        self.uploaded.append((backup, size, b"".join([chunk async for chunk in body])))

    async def download(self, backup_id):
        self.check()

        async def chunks():
            yield b"abc"
            if self.stream_failure:
                raise self.stream_failure
            yield b"def"

        return chunks()

    async def delete(self, backup_id) -> None:
        self.check()
        self.deleted.append(backup_id)


async def _open_stream():
    async def chunks():
        yield b"abc"
        yield b"def"

    return chunks()


class BackupAgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.entry = _Entry()
        self.hass = _Hass([self.entry])
        self.agent = agent_module.MaxisCloudBackupAgent(self.hass, self.entry)
        self.client = _Client()
        self.real_client = self.agent._client
        self.agent._client = self.client

    async def test_offered_only_while_the_entry_is_loaded(self) -> None:
        agents = await agent_module.async_get_backup_agents(self.hass)
        self.assertEqual([agent.agent_id for agent in agents], ["rexlite.maxis_cloud"])
        self.assertEqual(agents[0].name, "MAX'Is 雲端")
        self.assertEqual(await agent_module.async_get_backup_agents(_Hass([])), [])

    def test_listeners_come_and_go(self) -> None:
        heard: list[str] = []
        remove = agent_module.async_register_backup_agents_listener(
            self.hass, listener=lambda: heard.append("x")
        )
        self.assertEqual(len(self.hass.data[package.DATA_BACKUP_AGENT_LISTENERS]), 1)
        remove()
        remove()
        self.assertNotIn(package.DATA_BACKUP_AGENT_LISTENERS, self.hass.data)

    def test_credentials_follow_the_entry(self) -> None:
        credentials = self.real_client._credentials
        expected = cloud.CloudBackupCredentials("hub-1", "token-1")
        self.assertEqual(credentials(), expected)
        self.entry.data = {"agent_id": "hub-1", "agent_auth_token": "rotated"}
        self.assertEqual(credentials().token, "rotated")
        self.assertTrue(self.real_client._base_url.endswith("/api/v1/hub-cloud-backup"))

    async def test_upload_sends_the_backup_and_reports_progress(self) -> None:
        progress: list[int] = []
        backup = _AgentBackup.from_dict(BACKUP)
        await self.agent.async_upload_backup(
            open_stream=_open_stream,
            backup=backup,
            on_progress=lambda *, bytes_uploaded: progress.append(bytes_uploaded),
        )
        self.assertEqual(self.client.uploaded, [(BACKUP, 6, b"abcdef")])
        self.assertEqual(progress, [3, 6])

        self.client.failure = cloud.CloudBackupError("此中樞主機未訂閱「雲端備份」")
        with self.assertRaises(_BackupAgentError) as caught:
            await self.agent.async_upload_backup(
                open_stream=_open_stream, backup=backup, on_progress=lambda **_: None
            )
        self.assertEqual(
            str(caught.exception), "MAX'Is 雲端：此中樞主機未訂閱「雲端備份」"
        )

    async def test_list_skips_what_is_not_a_backup(self) -> None:
        self.client.listed = [BACKUP, {"backup_id": "half"}]
        backups = await self.agent.async_list_backups()
        self.assertEqual([backup.backup_id for backup in backups], ["a1b2c3d4"])
        self.assertEqual((await self.agent.async_get_backup("a1b2c3d4")).size, 6)
        with self.assertRaises(_BackupNotFound):
            await self.agent.async_get_backup("missing")
        self.client.failure = cloud.CloudBackupError("down")
        with self.assertRaises(_BackupAgentError):
            await self.agent.async_list_backups()

    async def test_download_and_delete_map_missing_and_broken(self) -> None:
        stream = await self.agent.async_download_backup("a1b2c3d4")
        self.assertEqual(b"".join([chunk async for chunk in stream]), b"abcdef")

        self.client.stream_failure = cloud.CloudBackupError("cut")
        stream = await self.agent.async_download_backup("a1b2c3d4")
        with self.assertRaises(_BackupAgentError):
            _ = [chunk async for chunk in stream]

        await self.agent.async_delete_backup("a1b2c3d4")
        self.assertEqual(self.client.deleted, ["a1b2c3d4"])
        self.client.failure = cloud.CloudBackupNotFound("gone", status=404)
        with self.assertRaises(_BackupNotFound):
            await self.agent.async_download_backup("a1b2c3d4")
        with self.assertRaises(_BackupNotFound):
            await self.agent.async_delete_backup("a1b2c3d4")
        self.client.failure = cloud.CloudBackupError("down")
        with self.assertRaises(_BackupAgentError):
            await self.agent.async_delete_backup("a1b2c3d4")


if __name__ == "__main__":
    unittest.main()
