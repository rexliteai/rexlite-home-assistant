"""MAX'Is 雲端 client over real aiohttp, against a local platform and storage.

Needs aiohttp installed; run on its own:
    python -m unittest tests/cloud_backup_runtime_check.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

PACKAGE = Path(__file__).parents[1] / "custom_components/rexlite"
package = types.ModuleType("rexlite_cloud_backup_tests")
package.__path__ = [str(PACKAGE)]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location(
    f"{package.__name__}.cloud_backup", PACKAGE / "cloud_backup.py"
)
cloud = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = cloud
spec.loader.exec_module(cloud)

BACKUP = {
    "addons": [],
    "backup_id": "a1b2c3d4",
    "date": "2026-11-13T03:00:00.000000+08:00",
    "database_included": True,
    "extra_metadata": {"with_automatic_settings": True},
    "folders": [],
    "homeassistant_included": True,
    "homeassistant_version": "2026.8.0",
    "name": "Automatic backup 2026.8.0",
    "protected": True,
    "size": 10,
}


class Platform:
    """The operation backend's hub API and a storage upload address."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.stored: dict[str, bytes] = {}
        self.refuse_upload: tuple[int, dict[str, Any]] | None = None
        self.server: TestServer | None = None

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/api/v1/hub-cloud-backup/backups", self.list)
        app.router.add_post(
            "/api/v1/hub-cloud-backup/backups/{backup_id}/upload", self.upload
        )
        app.router.add_get(
            "/api/v1/hub-cloud-backup/backups/{backup_id}/download", self.download
        )
        app.router.add_delete(
            "/api/v1/hub-cloud-backup/backups/{backup_id}", self.delete
        )
        app.router.add_put("/storage/{backup_id}", self.store)
        return app

    def note(self, request: web.Request) -> None:
        self.requests.append(
            {
                "method": request.method,
                "path": request.raw_path,
                "authorization": request.headers.get("Authorization"),
                "agent": request.headers.get("X-REXLiTE-Agent-Id"),
                "user_agent": request.headers.get("User-Agent"),
            }
        )

    async def list(self, request: web.Request) -> web.Response:
        self.note(request)
        backups = [BACKUP] if "a1b2c3d4" in self.stored else []
        return web.json_response(
            {"success": True, "data": {"backups": [*backups, "not a backup"]}}
        )

    async def upload(self, request: web.Request) -> web.Response:
        self.note(request)
        if self.refuse_upload:
            status, body = self.refuse_upload
            return web.json_response(body, status=status)
        body = await request.json()
        self.requests[-1]["body"] = body
        assert self.server is not None
        url = self.server.make_url(f"/storage/{request.match_info['backup_id']}")
        return web.json_response({"success": True, "data": {"uploadUrl": str(url)}})

    async def store(self, request: web.Request) -> web.Response:
        self.requests.append(
            {
                "method": "PUT",
                "path": request.path,
                "length": request.headers.get("Content-Length"),
                "chunked": request.headers.get("Transfer-Encoding"),
                "authorization": request.headers.get("Authorization"),
            }
        )
        self.stored[request.match_info["backup_id"]] = await request.read()
        return web.Response(status=200)

    async def download(self, request: web.Request) -> web.StreamResponse:
        self.note(request)
        data = self.stored.get(request.match_info["backup_id"])
        if data is None:
            return web.json_response(
                {
                    "success": False,
                    "error": {"code": "HUB_CLOUD_BACKUP_NOT_FOUND", "message": "無"},
                },
                status=404,
            )
        response = web.StreamResponse(headers={"Content-Type": "application/x-tar"})
        await response.prepare(request)
        for start in range(0, len(data), 4):
            await response.write(data[start : start + 4])
        await response.write_eof()
        return response

    async def delete(self, request: web.Request) -> web.Response:
        self.note(request)
        if self.stored.pop(request.match_info["backup_id"], None) is None:
            return web.json_response({"code": "NOPE", "message": "gone"}, status=404)
        return web.json_response({"success": True, "data": {"deleted": True}})


class CloudBackupClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.platform = Platform()
        self.server = TestServer(self.platform.app())
        await self.server.start_server()
        self.platform.server = self.server
        self.session = aiohttp.ClientSession()
        self.token = "agent-token"
        self.client = cloud.MaxisCloudBackupClient(
            self.session,
            base_url=str(self.server.make_url("/api/v1/hub-cloud-backup")),
            credentials=lambda: cloud.CloudBackupCredentials("hub-1", self.token),
            user_agent="REXLiTE-AI/test",
        )

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.server.close()

    async def body(self, opened: list[str], data: bytes = b"0123456789"):
        opened.append("opened")

        async def chunks():
            for start in range(0, len(data), 3):
                yield data[start : start + 3]

        return chunks()

    async def test_upload_goes_straight_to_storage_with_its_exact_size(self) -> None:
        opened: list[str] = []
        await self.client.upload(BACKUP, 10, lambda: self.body(opened))

        self.assertEqual(self.platform.stored["a1b2c3d4"], b"0123456789")
        accepted, stored = self.platform.requests
        self.assertEqual(accepted["authorization"], "Bearer agent-token")
        self.assertEqual(accepted["agent"], "hub-1")
        self.assertEqual(accepted["user_agent"], "REXLiTE-AI/test")
        self.assertEqual(accepted["body"], {"backup": BACKUP, "size": 10})
        # Storage gets the file with its length, not chunked, and no agent token.
        self.assertEqual(stored["length"], "10")
        self.assertIsNone(stored["chunked"])
        self.assertIsNone(stored["authorization"])

    async def test_a_refused_upload_never_opens_the_backup(self) -> None:
        self.platform.refuse_upload = (
            403,
            {
                "success": False,
                "error": {
                    "code": "HUB_CLOUD_BACKUP_NOT_SUBSCRIBED",
                    "message": "此中樞主機未訂閱「雲端備份」",
                },
            },
        )
        opened: list[str] = []
        with self.assertRaises(cloud.CloudBackupError) as caught:
            await self.client.upload(BACKUP, 10, lambda: self.body(opened))
        self.assertEqual(caught.exception.code, "HUB_CLOUD_BACKUP_NOT_SUBSCRIBED")
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(str(caught.exception), "此中樞主機未訂閱「雲端備份」")
        self.assertEqual(opened, [])

    async def test_list_download_and_delete(self) -> None:
        self.assertEqual(await self.client.list_backups(), [])
        await self.client.upload(BACKUP, 10, lambda: self.body([]))
        self.assertEqual(await self.client.list_backups(), [BACKUP])

        stream = await self.client.download("a1b2c3d4")
        self.assertEqual(b"".join([chunk async for chunk in stream]), b"0123456789")

        await self.client.delete("a1b2c3d4")
        with self.assertRaises(cloud.CloudBackupNotFound) as caught:
            await self.client.delete("a1b2c3d4")
        self.assertEqual(caught.exception.code, "NOPE")
        with self.assertRaises(cloud.CloudBackupNotFound):
            await self.client.download("a1b2c3d4")

    async def test_a_rotated_token_is_used_at_once_and_ids_are_escaped(self) -> None:
        self.token = "rotated"
        with self.assertRaises(cloud.CloudBackupNotFound):
            await self.client.download("../x")
        last = self.platform.requests[-1]
        self.assertEqual(last["authorization"], "Bearer rotated")
        self.assertIn("/backups/..%2Fx/download", last["path"])

    async def test_an_unreachable_platform_is_a_cloud_error(self) -> None:
        await self.server.close()
        with self.assertRaises(cloud.CloudBackupError):
            await self.client.list_backups()

    def test_error_shapes(self) -> None:
        self.assertEqual(
            cloud._failure({"error": {"code": "A", "message": "b"}}), ("A", "b")
        )
        self.assertEqual(cloud._failure({"code": "C", "message": "d"}), ("C", "d"))
        self.assertEqual(cloud._failure("x"), ("", ""))
        self.assertEqual(cloud._unwrap({"success": True, "data": {"a": 1}}), {"a": 1})
        self.assertEqual(cloud._unwrap({"a": 1}), {"a": 1})
        self.assertEqual(json.loads(json.dumps(BACKUP)), BACKUP)


if __name__ == "__main__":
    unittest.main()
