"""MAX'Is 雲端: REXLiTE AI's location for Home Assistant backups.

Home Assistant itself sends each backup made for this location (manual and
automatic, encrypted with the hub's key) and fetches one back to restore it.
The MAX'Is platform keeps the files; see cloud_backup.py. The location exists
while the REXLiTE AI entry is loaded; the platform switches it on or off in
the automatic-backup settings.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any

from homeassistant.components.backup import (
    AgentBackup,
    BackupAgent,
    BackupAgentError,
    BackupNotFound,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import DATA_BACKUP_AGENT_LISTENERS, REXLiTEConfigEntry
from .cloud_backup import (
    CloudBackupCredentials,
    CloudBackupError,
    CloudBackupNotFound,
    MaxisCloudBackupClient,
)
from .const import (
    CONF_AGENT_AUTH_TOKEN,
    CONF_AGENT_ID,
    DOMAIN,
    INTEGRATION_VERSION,
    MAXIS_CLOUD_BACKUP_NAME,
    MAXIS_CLOUD_BACKUP_UNIQUE_ID,
    MAXIS_CLOUD_BACKUP_URL,
)


async def async_get_backup_agents(
    hass: HomeAssistant, **kwargs: Any
) -> list[BackupAgent]:
    """MAX'Is 雲端 while the REXLiTE AI entry is loaded."""

    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        return []
    return [MaxisCloudBackupAgent(hass, entries[0])]


@callback
def async_register_backup_agents_listener(
    hass: HomeAssistant,
    *,
    listener: Callable[[], None],
    **kwargs: Any,
) -> Callable[[], None]:
    """Tell HA when MAX'Is 雲端 comes or goes with the REXLiTE AI entry."""

    listeners = hass.data.setdefault(DATA_BACKUP_AGENT_LISTENERS, [])
    listeners.append(listener)

    @callback
    def remove_listener() -> None:
        if listener in listeners:
            listeners.remove(listener)
        if not listeners:
            hass.data.pop(DATA_BACKUP_AGENT_LISTENERS, None)

    return remove_listener


class MaxisCloudBackupAgent(BackupAgent):
    """One hub's backups in MAX'Is 雲端 (agent rexlite.maxis_cloud)."""

    domain = DOMAIN
    name = MAXIS_CLOUD_BACKUP_NAME
    unique_id = MAXIS_CLOUD_BACKUP_UNIQUE_ID

    def __init__(self, hass: HomeAssistant, entry: REXLiTEConfigEntry) -> None:
        super().__init__()

        def credentials() -> CloudBackupCredentials:
            # Read each time: a rotated agent token is used at once.
            return CloudBackupCredentials(
                agent_id=str(entry.data[CONF_AGENT_ID]),
                token=str(entry.data[CONF_AGENT_AUTH_TOKEN]),
            )

        self._client = MaxisCloudBackupClient(
            async_get_clientsession(hass),
            base_url=MAXIS_CLOUD_BACKUP_URL,
            credentials=credentials,
            user_agent=f"REXLiTE-AI/{INTEGRATION_VERSION}",
        )

    async def async_upload_backup(
        self,
        *,
        open_stream: Callable[[], Coroutine[Any, Any, AsyncIterator[bytes]]],
        backup: AgentBackup,
        on_progress: Callable[..., None] | None = None,
        **kwargs: Any,
    ) -> None:
        async def open_body() -> AsyncIterator[bytes]:
            stream = await open_stream()

            async def counted() -> AsyncIterator[bytes]:
                uploaded = 0
                async for chunk in stream:
                    yield chunk
                    uploaded += len(chunk)
                    if on_progress is not None:
                        on_progress(bytes_uploaded=uploaded)

            return counted()

        try:
            await self._client.upload(backup.as_dict(), backup.size, open_body)
        except CloudBackupError as err:
            raise BackupAgentError(f"{MAXIS_CLOUD_BACKUP_NAME}：{err}") from err

    async def async_download_backup(
        self, backup_id: str, **kwargs: Any
    ) -> AsyncIterator[bytes]:
        try:
            stream = await self._client.download(backup_id)
        except CloudBackupNotFound as err:
            raise BackupNotFound(f"Backup {backup_id} not found") from err
        except CloudBackupError as err:
            raise BackupAgentError(f"{MAXIS_CLOUD_BACKUP_NAME}：{err}") from err

        async def chunks() -> AsyncIterator[bytes]:
            try:
                async for chunk in stream:
                    yield chunk
            except CloudBackupError as err:
                raise BackupAgentError(f"{MAXIS_CLOUD_BACKUP_NAME}：{err}") from err

        return chunks()

    async def async_delete_backup(self, backup_id: str, **kwargs: Any) -> None:
        try:
            await self._client.delete(backup_id)
        except CloudBackupNotFound as err:
            raise BackupNotFound(f"Backup {backup_id} not found") from err
        except CloudBackupError as err:
            raise BackupAgentError(f"{MAXIS_CLOUD_BACKUP_NAME}：{err}") from err

    async def async_list_backups(self, **kwargs: Any) -> list[AgentBackup]:
        # Never cached: a copy the platform just let this hub restore
        # (換機還原) must show at once.
        try:
            listed = await self._client.list_backups()
        except CloudBackupError as err:
            raise BackupAgentError(f"{MAXIS_CLOUD_BACKUP_NAME}：{err}") from err
        backups: list[AgentBackup] = []
        for item in listed:
            try:
                backups.append(AgentBackup.from_dict(item))
            except (KeyError, TypeError, ValueError):
                continue
        return backups

    async def async_get_backup(self, backup_id: str, **kwargs: Any) -> AgentBackup:
        for backup in await self.async_list_backups():
            if backup.backup_id == backup_id:
                return backup
        raise BackupNotFound(f"Backup {backup_id} not found")
