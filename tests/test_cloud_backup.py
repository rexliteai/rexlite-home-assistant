"""MAX'Is 雲端 client rules, with aiohttp stubbed like the other unit tests.

tests/cloud_backup_runtime_check.py runs the same client over real aiohttp.
"""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from typing import Any

PACKAGE = Path(__file__).parents[1] / "custom_components/rexlite"


class _ClientError(Exception):
    """Stub aiohttp transport error."""


class _ClientTimeout:
    def __init__(self, **kwargs: Any) -> None:
        self.settings = kwargs


aiohttp = types.ModuleType("aiohttp")
aiohttp.ClientError = _ClientError
aiohttp.ClientTimeout = _ClientTimeout
aiohttp.ClientSession = object
aiohttp.ClientResponse = object
sys.modules["aiohttp"] = aiohttp

package = types.ModuleType("rexlite_cloud_backup_unit_tests")
package.__path__ = [str(PACKAGE)]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location(
    f"{package.__name__}.cloud_backup", PACKAGE / "cloud_backup.py"
)
cloud = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = cloud
spec.loader.exec_module(cloud)

BASE = "https://operation-api.example/api/v1/hub-cloud-backup"
UPLOAD = "https://storage.example/upload?upload_id=abc"
BACKUP = {"backup_id": "a1b2c3d4", "name": "Automatic backup", "size": 6}


class _Content:
    def __init__(self, chunks: list[bytes], failure: Exception | None) -> None:
        self.chunks = chunks
        self.failure = failure

    async def iter_chunked(self, size: int):
        for chunk in self.chunks:
            yield chunk
        if self.failure:
            raise self.failure


class _Response:
    def __init__(
        self,
        status: int,
        payload: Any = None,
        chunks: list[bytes] | None = None,
        stream_failure: Exception | None = None,
    ) -> None:
        self.status = status
        self.payload = payload
        self.content = _Content(chunks or [], stream_failure)
        self.released = False

    async def json(self, content_type: Any = None) -> Any:
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    def release(self) -> None:
        self.released = True

    async def __aenter__(self) -> _Response:
        return self

    async def __aexit__(self, *args: Any) -> None:
        self.released = True


class _Session:
    """Answers the client's calls in order; records what it sent."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []
        self.put_bodies: list[bytes] = []

    def _next(self) -> Any:
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def request(self, method: str, url: str, **kwargs: Any) -> _Response:
        self.calls.append({"method": method, "url": url, **kwargs})
        return self._next()

    def put(self, url: str, **kwargs: Any) -> Any:
        self.calls.append({"method": "PUT", "url": url, **kwargs})
        session = self

        class _Upload:
            async def __aenter__(self) -> _Response:
                body = kwargs["data"]
                session.put_bodies.append(b"".join([chunk async for chunk in body]))
                return session._next()

            async def __aexit__(self, *args: Any) -> None:
                return None

        return _Upload()


def _client(session: _Session, base: str = BASE) -> Any:
    return cloud.MaxisCloudBackupClient(
        session,
        base_url=base + "/",
        credentials=lambda: cloud.CloudBackupCredentials("hub-1", "agent-token"),
        user_agent="REXLiTE-AI/test",
    )


async def _body(opened: list[str]):
    opened.append("opened")

    async def chunks():
        yield b"abc"
        yield b"def"

    return chunks()


def _ok(data: Any) -> _Response:
    return _Response(200, {"success": True, "data": data})


class CloudBackupClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_asks_the_platform_then_sends_to_storage(self) -> None:
        session = _Session(_ok({"uploadUrl": UPLOAD}), _Response(200))
        opened: list[str] = []
        await _client(session).upload(BACKUP, 6, lambda: _body(opened))

        accepted, stored = session.calls
        self.assertEqual(accepted["method"], "POST")
        self.assertEqual(accepted["url"], f"{BASE}/backups/a1b2c3d4/upload")
        self.assertEqual(accepted["json"], {"backup": BACKUP, "size": 6})
        self.assertEqual(
            accepted["headers"],
            {
                "Authorization": "Bearer agent-token",
                "X-REXLiTE-Agent-Id": "hub-1",
                "Accept": "application/json",
                "User-Agent": "REXLiTE-AI/test",
            },
        )
        self.assertFalse(accepted["allow_redirects"])
        self.assertEqual(accepted["timeout"].settings, {"total": 30})
        # Storage gets the exact length and never the agent credential.
        self.assertEqual(stored["url"], UPLOAD)
        self.assertEqual(
            stored["headers"],
            {"Content-Length": "6", "Content-Type": "application/x-tar"},
        )
        self.assertEqual(
            stored["timeout"].settings,
            {"total": None, "sock_connect": 30, "sock_read": 300},
        )
        self.assertEqual(session.put_bodies, [b"abcdef"])
        self.assertEqual(opened, ["opened"])

    async def test_a_refused_upload_never_opens_the_backup(self) -> None:
        refusal = _Response(
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
            await _client(_Session(refusal)).upload(BACKUP, 6, lambda: _body(opened))
        self.assertEqual(caught.exception.code, "HUB_CLOUD_BACKUP_NOT_SUBSCRIBED")
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(str(caught.exception), "此中樞主機未訂閱「雲端備份」")
        self.assertEqual(opened, [])
        self.assertTrue(refusal.released)

    async def test_upload_needs_an_https_storage_address(self) -> None:
        for answer in ({"uploadUrl": "http://storage.example/x"}, {}):
            opened: list[str] = []
            with self.assertRaises(cloud.CloudBackupError):
                await _client(_Session(_ok(answer))).upload(
                    BACKUP, 6, lambda opened=opened: _body(opened)
                )
            self.assertEqual(opened, [])

    async def test_storage_failures_are_cloud_errors(self) -> None:
        for answer in (_Response(503), _ClientError("reset")):
            session = _Session(_ok({"uploadUrl": UPLOAD}), answer)
            with self.assertRaises(cloud.CloudBackupError):
                await _client(session).upload(BACKUP, 6, lambda: _body([]))

    async def test_list_reads_the_platform_envelope(self) -> None:
        session = _Session(_ok({"backups": [BACKUP, "not a backup"]}))
        self.assertEqual(await _client(session).list_backups(), [BACKUP])
        self.assertEqual(session.calls[0]["url"], f"{BASE}/backups")
        with self.assertRaises(cloud.CloudBackupError):
            await _client(_Session(_ok({}))).list_backups()
        with self.assertRaises(cloud.CloudBackupError):
            await _client(_Session(_Response(200, ValueError("html")))).list_backups()
        with self.assertRaises(cloud.CloudBackupError) as caught:
            await _client(_Session(_ClientError("dns"))).list_backups()
        self.assertEqual(str(caught.exception), "MAX'Is 雲端 is unreachable")

    async def test_download_streams_and_releases(self) -> None:
        response = _Response(200, chunks=[b"abc", b"def"])
        session = _Session(response)
        stream = await _client(session).download("a/b")
        self.assertEqual(b"".join([chunk async for chunk in stream]), b"abcdef")
        self.assertTrue(response.released)
        self.assertEqual(session.calls[0]["url"], f"{BASE}/backups/a%2Fb/download")
        self.assertEqual(session.calls[0]["headers"]["Accept"], "application/x-tar")

        broken = _Response(200, chunks=[b"abc"], stream_failure=_ClientError("cut"))
        stream = await _client(_Session(broken)).download("a1b2c3d4")
        with self.assertRaises(cloud.CloudBackupError):
            _ = [chunk async for chunk in stream]
        self.assertTrue(broken.released)

    async def test_missing_backups_are_not_found(self) -> None:
        missing = _Response(
            404, {"code": "HUB_CLOUD_BACKUP_NOT_FOUND", "message": "無"}
        )
        with self.assertRaises(cloud.CloudBackupNotFound) as caught:
            await _client(_Session(missing)).download("a1b2c3d4")
        self.assertEqual(caught.exception.code, "HUB_CLOUD_BACKUP_NOT_FOUND")
        with self.assertRaises(cloud.CloudBackupNotFound):
            await _client(_Session(_Response(404, None))).delete("a1b2c3d4")
        session = _Session(_ok({"deleted": True}))
        await _client(session).delete("a1b2c3d4")
        self.assertEqual(session.calls[0]["method"], "DELETE")

    async def test_plain_http_storage_only_beside_a_plain_http_platform(self) -> None:
        session = _Session(_ok({"uploadUrl": "http://127.0.0.1:9/x"}), _Response(201))
        await _client(session, "http://127.0.0.1:9/api").upload(
            BACKUP, 6, lambda: _body([])
        )
        self.assertEqual(session.put_bodies, [b"abcdef"])


if __name__ == "__main__":
    unittest.main()
