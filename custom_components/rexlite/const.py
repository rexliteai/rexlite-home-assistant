"""Constants for the REXLiTE integration."""

from __future__ import annotations

from typing import Final

from homeassistant.const import Platform

DOMAIN: Final = "rexlite"

CONF_AGENT_ID: Final = "agent_id"
CONF_AGENT_AUTH_TOKEN: Final = "agent_auth_token"
CONF_GATEWAY_WS_URL: Final = "gateway_ws_url"
CONF_HOME_ASSISTANT_URL: Final = "home_assistant_url"
CONF_REMOTE_ADMIN_ENABLED: Final = "remote_admin_enabled"

DEFAULT_GATEWAY_WS_URL: Final = "wss://www.tunnel.maxisappai.com/ws/agent"
DEFAULT_HOME_ASSISTANT_URL: Final = "http://127.0.0.1:8123"
DEFAULT_REMOTE_ADMIN_ENABLED: Final = False

PLATFORMS: Final = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.SWITCH]
INTEGRATION_VERSION: Final = "0.1.56"

# MAX'Is 雲端: this hub's backup location in the MAX'Is cloud (agent
# "rexlite.maxis_cloud"). The operation platform keeps the files and accepts
# the hub by its REXLiTE AI agent credential.
MAXIS_CLOUD_BACKUP_URL: Final = (
    "https://operation-api.maxisappai.com/api/v1/hub-cloud-backup"
)
MAXIS_CLOUD_BACKUP_NAME: Final = "MAX'Is 雲端"
MAXIS_CLOUD_BACKUP_UNIQUE_ID: Final = "maxis_cloud"

ATTR_ACCESS_MODE: Final = "access_mode"
ATTR_GATEWAY_URL: Final = "gateway_url"
ATTR_LAST_CONNECTED_AT: Final = "last_connected_at"
ATTR_LAST_ERROR: Final = "last_error"
ATTR_RECONNECT_ATTEMPT: Final = "reconnect_attempt"

ACCESS_MODE_FULL_CONTROL: Final = "full_control"
ACCESS_MODE_HEALTH_ONLY: Final = "health_only"
