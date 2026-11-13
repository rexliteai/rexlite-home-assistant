"""MAX'Is 雲端 backup files: the operation platform's API for this hub.

The platform keeps each Home Assistant backup this hub sends as one file in
Google Cloud Storage and accepts the hub by its REXLiTE AI agent credential (the
one the tunnel uses). A file goes straight to storage through a one-time upload
address the platform hands out, so a large backup never passes through the
platform; it comes back through the platform as a stream. Nothing here reads or
needs the backup's encryption key: encrypted backups stay encrypted.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import quote

import aiohttp

AGENT_ID_HEADER: Final = "X-REXLiTE-Agent-Id"
_CHUNK_SIZE: Final = 1024 * 1024


def _api_timeout() -> aiohttp.ClientTimeout:
    return aiohttp.ClientTimeout(total=30)


def _transfer_timeout() -> aiohttp.ClientTimeout:
    # A transfer may run long; only one that stalls fails.
    return aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=300)


class CloudBackupError(Exception):
    """MAX'Is 雲端 refused the request or could not be reached."""

    def __init__(self, message: str, *, code: str = "", status: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class CloudBackupNotFound(CloudBackupError):
    """MAX'Is 雲端 holds no such backup for this hub."""


@dataclass(frozen=True, slots=True)
class CloudBackupCredentials:
    """This hub's REXLiTE AI agent, as the tunnel knows it."""

    agent_id: str
    token: str


def _unwrap(payload: Any) -> dict[str, Any]:
    """The platform answers `{success, data}`; older routes answer bare JSON."""

    if isinstance(payload, dict) and payload.get("success") is True:
        data = payload.get("data")
        return data if isinstance(data, dict) else {}
    return payload if isinstance(payload, dict) else {}


def _failure(payload: Any) -> tuple[str, str]:
    """(code, message) of a refused request, in either error shape."""

    if not isinstance(payload, dict):
        return "", ""
    error = payload.get("error")
    source = error if isinstance(error, dict) else payload
    code = source.get("code")
    message = source.get("message")
    return (
        code if isinstance(code, str) else "",
        message if isinstance(message, str) else "",
    )


def _path_id(backup_id: str) -> str:
    return quote(backup_id, safe="")


class MaxisCloudBackupClient:
    """Lists, sends, fetches and deletes this hub's MAX'Is 雲端 backups."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        base_url: str,
        credentials: Callable[[], CloudBackupCredentials],
        user_agent: str,
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._credentials = credentials
        self._user_agent = user_agent

    async def list_backups(self) -> list[dict[str, Any]]:
        """This hub's backups, and another hub's it may restore (換機還原)."""

        backups = (await self._json("GET", "/backups")).get("backups")
        if not isinstance(backups, list):
            raise CloudBackupError("MAX'Is 雲端 answered without a backup list")
        return [backup for backup in backups if isinstance(backup, dict)]

    async def upload(
        self,
        backup: dict[str, Any],
        size: int,
        open_body: Callable[[], Awaitable[AsyncIterator[bytes]]],
    ) -> None:
        """Sends one backup file of exactly `size` bytes.

        The body is opened only once the platform accepted the backup.
        """

        backup_id = str(backup.get("backup_id", ""))
        answer = await self._json(
            "POST",
            f"/backups/{_path_id(backup_id)}/upload",
            json={"backup": backup, "size": size},
        )
        upload_url = answer.get("uploadUrl")
        # Storage is HTTPS; plain HTTP only when the platform itself is (tests).
        secure = isinstance(upload_url, str) and (
            upload_url.startswith("https://")
            or (
                self._base_url.startswith("http://")
                and upload_url.startswith("http://")
            )
        )
        if not secure:
            raise CloudBackupError("MAX'Is 雲端 gave no upload address")
        body = await open_body()
        try:
            async with self._session.put(
                upload_url,
                data=body,
                headers={
                    "Content-Length": str(size),
                    "Content-Type": "application/x-tar",
                },
                timeout=_transfer_timeout(),
            ) as response:
                if response.status not in (200, 201):
                    raise CloudBackupError(
                        f"MAX'Is 雲端 storage refused the file ({response.status})",
                        status=response.status,
                    )
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CloudBackupError("MAX'Is 雲端 upload was interrupted") from err

    async def download(self, backup_id: str) -> AsyncIterator[bytes]:
        """The backup file as stored (still encrypted when it was)."""

        response = await self._send(
            "GET",
            f"/backups/{_path_id(backup_id)}/download",
            client_timeout=_transfer_timeout(),
            accept="application/x-tar",
        )

        async def chunks() -> AsyncIterator[bytes]:
            try:
                async for chunk in response.content.iter_chunked(_CHUNK_SIZE):
                    yield chunk
            except (aiohttp.ClientError, TimeoutError) as err:
                raise CloudBackupError("MAX'Is 雲端 download was interrupted") from err
            finally:
                response.release()

        return chunks()

    async def delete(self, backup_id: str) -> None:
        await self._json("DELETE", f"/backups/{_path_id(backup_id)}")

    async def _json(
        self, method: str, path: str, *, json: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        response = await self._send(
            method, path, json=json, client_timeout=_api_timeout()
        )
        try:
            payload = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise CloudBackupError(
                "MAX'Is 雲端 answered in an unexpected format"
            ) from err
        finally:
            response.release()
        return _unwrap(payload)

    async def _send(
        self,
        method: str,
        path: str,
        *,
        client_timeout: aiohttp.ClientTimeout,
        json: dict[str, Any] | None = None,
        accept: str = "application/json",
    ) -> aiohttp.ClientResponse:
        credentials = self._credentials()
        headers = {
            "Authorization": f"Bearer {credentials.token}",
            AGENT_ID_HEADER: credentials.agent_id,
            "Accept": accept,
            "User-Agent": self._user_agent,
        }
        try:
            response = await self._session.request(
                method,
                f"{self._base_url}{path}",
                json=json,
                headers=headers,
                timeout=client_timeout,
                allow_redirects=False,
            )
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CloudBackupError("MAX'Is 雲端 is unreachable") from err
        if 200 <= response.status < 300:
            return response
        try:
            payload = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError):
            payload = None
        finally:
            response.release()
        code, message = _failure(payload)
        message = message or f"MAX'Is 雲端 refused the request ({response.status})"
        if response.status == 404:
            raise CloudBackupNotFound(message, code=code, status=404)
        raise CloudBackupError(message, code=code, status=response.status)
