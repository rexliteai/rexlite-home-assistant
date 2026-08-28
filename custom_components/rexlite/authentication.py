"""Credential validation for the REXLiTE cloud service."""

from __future__ import annotations

import aiohttp

from .protocol import credential_probe_url


class InvalidAuthError(Exception):
    """Raised when the service credential is rejected."""


class CannotConnectError(Exception):
    """Raised when the cloud service cannot be reached."""


async def async_validate_gateway_credentials(
    session: aiohttp.ClientSession,
    *,
    gateway_url: str,
    agent_id: str,
    token: str,
) -> None:
    """Validate credentials without mutating the remote connection."""

    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with session.get(
            credential_probe_url(gateway_url, agent_id),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            allow_redirects=False,
        ) as response:
            if response.status in (401, 403):
                raise InvalidAuthError
            # A valid agent credential can receive 404 when no Home Assistant
            # access credential has been stored yet. Both statuses prove that
            # the agent credential itself was accepted.
            if response.status not in (200, 404):
                raise CannotConnectError
    except InvalidAuthError:
        raise
    except (aiohttp.ClientError, TimeoutError) as err:
        raise CannotConnectError from err
