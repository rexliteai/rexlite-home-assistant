"""跳電紀錄: why Home Assistant restarted, with power cuts kept apart.

Each run records its host boot and the Home Assistant and custom integration
versions. A planned stop (restart, ``docker stop``, a host shutdown) fires
``homeassistant_stop`` and the run is stamped, with whoever called
``homeassistant.restart``/``stop`` just before. At the next start the end of
the previous run is explained:

- stamped: versions changed (update), a restart call (manual, by whom), the
  host booted again (host_reboot), otherwise a restart from the host system
  (restart).
- not stamped, same host boot: Home Assistant died while the host kept
  running (crash).
- not stamped, the host booted again: the hub lost power or was reset
  (reboot). Only when the site's internet gateway restarted in that window
  too is it a power cut (power). A gateway that stayed up means a person or a
  crash reset the hub; no gateway answering leaves it unknown.

A backup copies the record of the run making it; that run is marked, so a
restored backup reads as a restore instead of a cut.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import os
import time
from collections.abc import Awaitable, Callable, Coroutine
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_LOGGER = logging.getLogger(__name__)

DATA_KEY = "rexlite_power_events"
SCHEMA_VERSION = 1
MAX_EVENTS = 200
BACKFILL_DAYS = 30
MAX_BACKFILL = 50
RECORDER_TIMEOUT = 30
STOP_REQUEST_WINDOW = 600
SITE_ATTEMPTS = 20
SITE_RETRY = 30
SITE_MARGIN = timedelta(seconds=30)
IGD_TYPES = (
    "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
    "urn:schemas-upnp-org:device:InternetGatewayDevice:2",
)

POWER = "power"  # the hub and the site's gateway lost power: 跳電
REBOOT = "reboot"  # the hub alone went down hard; see "site"
CRASH = "crash"  # Home Assistant died, the host kept running
UPDATE = "update"  # stopped normally; HA or custom integrations changed
MANUAL = "manual"  # stopped normally on a restart/stop call; see "by"
RESTART = "restart"  # stopped normally from the host system (docker, scripts)
HOST_REBOOT = "host_reboot"  # stopped normally and the host booted again
RESTORE = "restore"  # started from a restored backup
UNCLEAN = "unclean"  # before this version tracked runs: cause unknown
KINDS = (POWER, REBOOT, CRASH, UPDATE, MANUAL, RESTART, HOST_REBOOT, RESTORE, UNCLEAN)

# "site" of a reboot: was the site's gateway down too?
CHECKING, SITE_UP, SITE_UNKNOWN = "checking", "up", "unknown"


def _component(name: str):
    # Not a static import: REXLiTE AI must load when this integration is
    # missing or failed its setup (tests/test_manifest_dependencies.py).
    return __import__(f"homeassistant.components.{name}", fromlist=("__name__",))


def to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="seconds")


def from_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def as_utc(value: Any) -> datetime | None:
    """Recorder DATETIME columns come back naive (UTC) on SQLite."""
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def host_boot(stat: str) -> datetime | None:
    """``btime`` of /proc/stat: the host kernel's boot, seen by every container."""
    for line in stat.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "btime" and parts[1].isdigit():
            return datetime.fromtimestamp(int(parts[1]), UTC)
    return None


def process_start(self_stat: str, boot: datetime, ticks: int) -> datetime | None:
    """Start of this process: /proc/self/stat field 22, clock ticks after boot."""
    # Field 2 is the command in parentheses and may contain spaces.
    try:
        fields = self_stat[self_stat.rindex(")") + 2 :].split()
        after_boot = int(fields[19])
    except (ValueError, IndexError):
        return None
    if after_boot < 0 or ticks <= 0:
        return None
    return boot + timedelta(seconds=after_boot / ticks)


def read_current_run(proc: Path = Path("/proc")) -> dict:
    """This Home Assistant run and the host boot it runs on (blocking reads)."""

    def read(name: str) -> str:
        try:
            return (proc / name).read_text(encoding="ascii", errors="replace")
        except OSError:
            return ""

    boot = host_boot(read("stat"))
    started = None
    if boot is not None:
        try:
            ticks = os.sysconf("SC_CLK_TCK")
        except (OSError, ValueError):
            ticks = 0
        started = process_start(read("self/stat"), boot, ticks)
    return {
        "startedAt": to_iso(started or datetime.now(UTC)),
        "hostBootAt": to_iso(boot),
        "bootId": read("sys/kernel/random/boot_id").strip() or None,
        "stoppingAt": None,
    }


def host_rebooted(previous: dict, current: dict) -> bool | None:
    """Whether the host booted again after the previous run started."""
    if previous.get("bootId") and current.get("bootId"):
        return previous["bootId"] != current["bootId"]
    started = from_iso(previous.get("startedAt"))
    boot = from_iso(current.get("hostBootAt"))
    if started is None or boot is None:
        return None
    return boot > started


def version_changes(previous: dict, current: dict) -> list[str]:
    """``name|from|to`` per change; an empty side is an install or removal."""
    changes = []
    before_ha, after_ha = previous.get("haVersion"), current.get("haVersion")
    if before_ha and after_ha and before_ha != after_ha:
        changes.append(f"Home Assistant|{before_ha}|{after_ha}")
    before, after = previous.get("integrations"), current.get("integrations")
    # Runs from before versions were kept have nothing to compare with.
    if isinstance(before, dict) and isinstance(after, dict) and before:
        for domain in sorted(set(before) | set(after)):
            old, new = before.get(domain) or {}, after.get(domain) or {}
            if old and new and old.get("version") == new.get("version"):
                continue
            name = str(new.get("name") or old.get("name") or domain).replace("|", "/")
            changes.append(
                f"{name}|{old.get('version') or ''}|{new.get('version') or ''}"
            )
    return changes[:10]


def make_event(
    event_id: str,
    kind: str,
    run_start: datetime,
    last_alive: datetime | None,
    restored: datetime | None,
    started: str | None,
) -> dict:
    # A write outside the run belongs to another run or to a wrong clock.
    if last_alive is not None and (
        last_alive < run_start or (restored is not None and last_alive > restored)
    ):
        last_alive = None
    event = {
        "id": event_id,
        "kind": kind,
        "lostAt": to_iso(last_alive),
        "restoredAt": to_iso(restored),
        "startedAt": started,
    }
    if kind == REBOOT:
        event["site"] = CHECKING
    return event


def explain_previous(
    previous: Any, current: dict, last_alive: datetime | None
) -> dict | None:
    """Why the previous run ended; None when there is nothing to tell."""
    if not isinstance(previous, dict):
        return None
    start = from_iso(previous.get("startedAt"))
    if start is None or previous.get("startedAt") == current.get("startedAt"):
        return None
    rebooted = host_rebooted(previous, current)
    boot, started = from_iso(current.get("hostBootAt")), current.get("startedAt")
    if previous.get("backupAt") and not previous.get("stoppingAt"):
        # This record came back with a restored backup (or HA died while
        # backing up): it cannot tell how its run ended.
        return make_event(
            previous["startedAt"], RESTORE, start, None, from_iso(started), started
        )
    if previous.get("stoppingAt"):
        changes = version_changes(previous, current)
        if changes:
            kind = UPDATE
        elif previous.get("stopBy"):
            kind = MANUAL
        elif rebooted:
            kind = HOST_REBOOT
        else:
            kind = RESTART
        event = make_event(
            previous["startedAt"],
            kind,
            start,
            from_iso(previous["stoppingAt"]),
            boot if rebooted else from_iso(started),
            started,
        )
        if previous.get("stopBy"):
            event["by"] = str(previous["stopBy"])
        if changes:
            event["changes"] = changes
        return event
    kind = UNCLEAN if rebooted is None else REBOOT if rebooted else CRASH
    restored = boot if kind == REBOOT else from_iso(started)
    return make_event(previous["startedAt"], kind, start, last_alive, restored, started)


def backfilled_events(runs: list[dict], current: dict) -> list[dict]:
    """Unclean recorder runs from before this version stamped shutdowns.

    Only the run right before this one can be told apart: the host boot is
    known for the current boot alone.
    """
    boot = from_iso(current.get("hostBootAt"))
    events = []
    for run in runs:
        start, end = run["start"], run["end"]
        kind, restored = UNCLEAN, end
        if run.get("latest") and boot is not None:
            kind = REBOOT if boot > start else CRASH
            restored = boot if kind == REBOOT else end
        events.append(
            make_event(
                to_iso(start), kind, start, run.get("lastAlive"), restored, to_iso(end)
            )
        )
    return events


def site_state(
    uptimes: list[int],
    now: datetime,
    lost_at: datetime | None,
    run_start: datetime | None,
) -> str:
    """POWER when a gateway came up after the hub went down, else up/unknown."""
    if not uptimes:
        return SITE_UNKNOWN
    since = [now - timedelta(seconds=uptime) for uptime in uptimes]
    if lost_at is not None:
        return POWER if any(s > lost_at - SITE_MARGIN for s in since) else SITE_UP
    if run_start is not None and all(s < run_start for s in since):
        return SITE_UP
    return SITE_UNKNOWN


def valid_event(event: Any) -> bool:
    return (
        isinstance(event, dict)
        and isinstance(event.get("id"), str)
        and bool(event["id"])
        and event.get("kind") in KINDS
        and all(
            event.get(key) is None or from_iso(event.get(key)) is not None
            for key in ("lostAt", "restoredAt", "startedAt")
        )
    )


def event_time(event: dict) -> str:
    return (
        to_iso(
            from_iso(event.get("lostAt"))
            or from_iso(event.get("restoredAt"))
            or from_iso(event.get("id"))
        )
        or ""
    )


def merge_events(new: list[dict], existing: list[dict]) -> list[dict]:
    """Newest first, one record per run, at most MAX_EVENTS."""
    by_id: dict[str, dict] = {}
    for event in [*existing, *new]:
        if valid_event(event):
            by_id[event["id"]] = event
    return sorted(by_id.values(), key=event_time, reverse=True)[:MAX_EVENTS]


def recorder_last_alive(
    session: Any, start: datetime, end: datetime
) -> datetime | None:
    """The last state or event the recorder committed in [start, end)."""
    from sqlalchemy import func, select

    schema = _component("recorder.db_schema")
    Events, States = schema.Events, schema.States

    low, high = start.timestamp(), end.timestamp()
    stamps = [
        session.execute(
            select(func.max(States.last_updated_ts)).where(
                States.last_updated_ts >= low, States.last_updated_ts < high
            )
        ).scalar(),
        session.execute(
            select(func.max(Events.time_fired_ts)).where(
                Events.time_fired_ts >= low, Events.time_fired_ts < high
            )
        ).scalar(),
    ]
    stamps = [s for s in stamps if isinstance(s, int | float)]
    return datetime.fromtimestamp(max(stamps), UTC) if stamps else None


def recorder_unclean_runs(session: Any, since: datetime) -> list[dict]:
    """Finished recorder runs HA closed as incorrect, newest first."""
    from sqlalchemy import select

    RecorderRuns = _component("recorder.db_schema").RecorderRuns

    rows = session.execute(
        select(RecorderRuns.start, RecorderRuns.end, RecorderRuns.closed_incorrect)
        .where(RecorderRuns.start >= since)
        .order_by(RecorderRuns.start.desc())
        .limit(200)
    ).all()
    finished = [
        (as_utc(start), as_utc(end), bool(incorrect))
        for start, end, incorrect in rows
        if as_utc(start) and as_utc(end)
    ]
    runs = []
    for index, (start, end, incorrect) in enumerate(finished):
        if incorrect:
            runs.append(
                {
                    "start": start,
                    "end": end,
                    "lastAlive": recorder_last_alive(session, start, end),
                    "latest": index == 0,
                }
            )
        if len(runs) >= MAX_BACKFILL:
            break
    return runs


RecorderQuery = Callable[[Callable[[Any], Any]], Awaitable[Any]]
GatewayQuery = Callable[[], Awaitable[list[int]]]


def recorder_query(hass: Any) -> RecorderQuery:
    """Run a read on the recorder's own executor; None when it is unavailable."""

    async def run(job: Callable[[Any], Any]) -> Any:
        try:
            from homeassistant.helpers.recorder import get_instance, session_scope

            instance = get_instance(hass)
        except (ImportError, KeyError):
            return None

        def read() -> Any:
            with session_scope(
                session=instance.get_session(), read_only=True
            ) as session:
                return job(session)

        try:
            return await asyncio.wait_for(
                instance.async_add_executor_job(read), RECORDER_TIMEOUT
            )
        except Exception:  # noqa: BLE001 - the record works without timestamps
            _LOGGER.debug("Recorder read for power events failed", exc_info=True)
            return None

    return run


def lan_location(location: str) -> bool:
    """Only follow SSDP answers that point into the local network."""
    try:
        address = ipaddress.ip_address(urlparse(location).hostname or "")
    except ValueError:
        return False
    return address.is_private or address.is_link_local


def gateway_query(hass: Any) -> GatewayQuery:
    """WAN uptime (seconds) of each UPnP internet gateway answering on the LAN."""

    async def run() -> list[int]:
        from async_upnp_client.aiohttp import AiohttpSessionRequester
        from async_upnp_client.client_factory import UpnpFactory
        from async_upnp_client.profiles.igd import IgdDevice
        from async_upnp_client.search import async_search
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        locations: set[str] = set()

        async def found(headers: Any) -> None:
            location = headers.get("location")
            if isinstance(location, str) and lan_location(location):
                locations.add(location)

        for target in IGD_TYPES:
            with contextlib.suppress(OSError):
                await async_search(
                    async_callback=found, timeout=4, search_target=target
                )
        requester = AiohttpSessionRequester(
            async_get_clientsession(hass), with_sleep=True, timeout=10
        )
        factory = UpnpFactory(requester, non_strict=True)
        uptimes = []
        for location in sorted(locations)[:4]:
            try:
                device = await factory.async_create_device(location)
                status = await IgdDevice(device, None).async_get_status_info()
            except Exception:  # noqa: BLE001 - a gateway that can't tell is skipped
                continue
            uptime = getattr(status, "uptime", None)
            if isinstance(uptime, int) and uptime >= 0:
                uptimes.append(uptime)
        return uptimes

    return run


class PowerEvents:
    def __init__(
        self,
        store: Any,
        current: dict,
        query: RecorderQuery,
        gateways: GatewayQuery | None = None,
        schedule: Callable[[Coroutine], Any] | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        user_name: Callable[[str], Awaitable[str]] | None = None,
    ) -> None:
        self.store, self.current, self.query = store, current, query
        self.gateways, self.sleep, self.user_name = gateways, sleep, user_name
        self.schedule = schedule or asyncio.ensure_future
        self.events: list[dict] = []
        self.lock = asyncio.Lock()
        self.started: asyncio.Future | None = None
        self.site_check: Any = None
        self.stop_request: tuple[str, float] | None = None

    async def start(self) -> None:
        async with self.lock:
            try:
                saved = await self.store.async_load()
            except Exception:  # noqa: BLE001 - history is optional; keep tracking
                _LOGGER.warning("Power event history could not be read; starting over")
                saved = None
            saved = saved if isinstance(saved, dict) else {}
            stored = saved.get("events")
            self.events = merge_events([], stored if isinstance(stored, list) else [])
            for event in self.events:
                # A check cut short by another restart can no longer be made.
                if event.get("site") == CHECKING:
                    event["site"] = SITE_UNKNOWN
            previous = saved.get("run")
            new: list[dict] = []
            if isinstance(previous, dict):
                new_event = await self.check_previous(previous)
                if new_event:
                    new.append(new_event)
            elif not saved:
                since = datetime.now(UTC) - timedelta(days=BACKFILL_DAYS)
                runs = await self.query(lambda s: recorder_unclean_runs(s, since))
                new.extend(backfilled_events(runs or [], self.current))
            self.events = merge_events(new, self.events)
            for event in new:
                _LOGGER.info(
                    "Previous Home Assistant run ended: %s, at %s, back %s",
                    event["kind"],
                    event["lostAt"] or "unknown",
                    event["restoredAt"] or "unknown",
                )
            await self.save()
            reboots = [e for e in new if e.get("site") == CHECKING]
        for event in reboots:
            lost_at, run_start = from_iso(event["lostAt"]), from_iso(event["id"])
            self.site_check = self.schedule(
                self.check_site(event["id"], lost_at, run_start)
            )

    async def check_previous(self, previous: dict) -> dict | None:
        start = from_iso(previous.get("startedAt"))
        now = from_iso(self.current.get("startedAt"))
        alive = None
        abrupt = not previous.get("stoppingAt") and not previous.get("backupAt")
        if abrupt and start and now and start < now:
            alive = await self.query(lambda s: recorder_last_alive(s, start, now))
        return explain_previous(previous, self.current, alive)

    async def check_site(
        self, event_id: str, lost_at: datetime | None, run_start: datetime | None
    ) -> None:
        """Whether the site's gateway went down with the hub: a power cut."""
        state = SITE_UNKNOWN
        for attempt in range(SITE_ATTEMPTS if self.gateways else 0):
            try:
                uptimes = await self.gateways()
            except Exception:  # noqa: BLE001 - treated as no answer
                _LOGGER.debug("Gateway uptime query failed", exc_info=True)
                uptimes = []
            if uptimes:
                state = site_state(uptimes, datetime.now(UTC), lost_at, run_start)
                break
            # After a power cut the gateway may still be booting.
            if attempt + 1 < SITE_ATTEMPTS:
                await self.sleep(SITE_RETRY)
        async with self.lock:
            for event in self.events:
                if event["id"] == event_id and event.get("site") == CHECKING:
                    if state == POWER:
                        event["kind"] = POWER
                        event.pop("site", None)
                    else:
                        event["site"] = state
            await self.save()

    def stop_requested(self, by: str) -> None:
        """A homeassistant.restart/stop call: a user id, @automation or @system."""
        self.stop_request = (by, time.monotonic())

    async def stopping(self, _event: Any = None) -> None:
        """Stamp the run: this stop was asked for, not a power cut."""
        async with self.lock:
            self.current["stoppingAt"] = to_iso(datetime.now(UTC))
            if self.stop_request and (
                time.monotonic() - self.stop_request[1] < STOP_REQUEST_WINDOW
            ):
                by = self.stop_request[0]
                if not by.startswith("@"):
                    name = None
                    if self.user_name:
                        with contextlib.suppress(Exception):
                            name = await self.user_name(by)
                    # Never keep a raw user id.
                    by = name or "@user"
                self.current["stopBy"] = by
            await self.save()

    async def backing_up(self, active: bool) -> None:
        """Mark the run while a backup copies this record (see backup.py)."""
        async with self.lock:
            if active:
                self.current["backupAt"] = to_iso(datetime.now(UTC))
            else:
                self.current.pop("backupAt", None)
            await self.save()

    async def save(self) -> None:
        try:
            await self.store.async_save({"run": self.current, "events": self.events})
        except Exception:  # noqa: BLE001 - never break startup or shutdown
            _LOGGER.warning("Power event history could not be saved", exc_info=True)

    async def snapshot(self) -> dict:
        if self.started is not None and not self.started.done():
            await asyncio.wait({self.started}, timeout=10)
        return {
            "schemaVersion": SCHEMA_VERSION,
            "startedAt": self.current.get("startedAt"),
            "hostBootAt": self.current.get("hostBootAt"),
            "events": [dict(event) for event in self.events],
        }


async def installed_versions(hass: Any) -> tuple[str, dict]:
    """Home Assistant's version and the custom integrations this run loaded."""
    from homeassistant.const import __version__
    from homeassistant.loader import async_get_custom_components

    integrations = {}
    with contextlib.suppress(Exception):
        for domain, integration in (await async_get_custom_components(hass)).items():
            integrations[domain] = {
                "name": str(integration.name or domain),
                "version": str(integration.version or ""),
            }
    return __version__, integrations


async def async_register_power_events(hass: Any) -> PowerEvents:
    import voluptuous as vol
    from homeassistant.components import websocket_api
    from homeassistant.const import EVENT_CALL_SERVICE, EVENT_HOMEASSISTANT_STOP
    from homeassistant.core import callback
    from homeassistant.helpers.storage import Store

    if DATA_KEY in hass.data:
        return hass.data[DATA_KEY]

    async def user_name(user_id: str) -> str:
        user = await hass.auth.async_get_user(user_id)
        return user.name if user and user.name else "@user"

    current = await hass.async_add_executor_job(read_current_run)
    current["haVersion"], current["integrations"] = await installed_versions(hass)
    monitor = PowerEvents(
        Store(hass, 1, DATA_KEY),
        current,
        recorder_query(hass),
        gateway_query(hass),
        lambda job: hass.async_create_background_task(job, "rexlite power site"),
        user_name=user_name,
    )
    hass.data[DATA_KEY] = monitor
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, monitor.stopping)

    @callback
    def restart_call(data: Any) -> bool:
        return data.get("domain") == "homeassistant" and data.get("service") in (
            "restart",
            "stop",
        )

    @callback
    def restart_requested(event: Any) -> None:
        # Synchronous: the stop that follows must already see who asked.
        context = event.context
        monitor.stop_requested(
            context.user_id or ("@automation" if context.parent_id else "@system")
        )

    hass.bus.async_listen(EVENT_CALL_SERVICE, restart_requested, restart_call)
    # The recorder read must not hold up the cloud tunnel starting.
    monitor.started = hass.async_create_background_task(
        monitor.start(), "rexlite power events"
    )

    @websocket_api.websocket_command({vol.Required("type"): "rexlite/power_events"})
    @websocket_api.require_admin
    @websocket_api.async_response
    async def read(hass, connection, msg):
        connection.send_result(msg["id"], await monitor.snapshot())

    websocket_api.async_register_command(hass, read)
    return monitor
