"""Real HA recorder schema, storage, stop event and WebSocket checks for 跳電紀錄."""

import asyncio
import importlib.util
import json
import logging
import sys
import tempfile
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from homeassistant.auth.models import User
from homeassistant.components.recorder.db_schema import (
    Base,
    Events,
    RecorderRuns,
    States,
)
from homeassistant.components.websocket_api.connection import ActiveConnection
from homeassistant.const import (
    EVENT_CALL_SERVICE,
    EVENT_HOMEASSISTANT_STOP,
    __version__,
)
from homeassistant.core import Context, HomeAssistant
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

BASE = Path(__file__).parents[1] / "custom_components/rexlite"
spec = importlib.util.spec_from_file_location(
    "power_runtime_check", BASE / "power_events.py"
)
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)

T0 = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(days=7)


def at(hours: float) -> datetime:
    return T0 + timedelta(hours=hours)


def recorder_database(path: Path):
    """HA's own tables; aware UTC values are stored naive, as HA does on SQLite."""
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                RecorderRuns(start=at(0), end=at(10), closed_incorrect=True),
                RecorderRuns(start=at(10), end=at(20), closed_incorrect=False),
                RecorderRuns(start=at(20), end=at(30), closed_incorrect=True),
                RecorderRuns(start=at(30), end=None, closed_incorrect=False),
            ]
        )
        for hours in (1, 9.5, 21, 29.25, 31, 40):
            session.add(States(state="on", last_updated_ts=at(hours).timestamp()))
        session.add(Events(time_fired_ts=at(29.5).timestamp(), origin_idx=0))
        session.commit()
    return engine


def current_run(hours: float, boot: float, boot_id: str) -> dict:
    return {
        "startedAt": m.to_iso(at(hours)),
        "hostBootAt": m.to_iso(at(boot)),
        "bootId": boot_id,
        "stoppingAt": None,
    }


ADMIN = User(name="Admin", perm_lookup=None, is_owner=True)


async def start_hass(directory: str, engine, run: dict, uptimes: list[int]):
    """A fresh HA process: this run, the test database, a fake LAN gateway."""
    hass = HomeAssistant(directory)
    hass.auth = types.SimpleNamespace(async_get_user=AsyncMock(return_value=ADMIN))

    async def query(job):
        def read():
            with Session(engine) as session:
                return job(session)

        return await hass.async_add_executor_job(read)

    async def gateway():
        return list(uptimes)

    with (
        patch.object(m, "recorder_query", lambda _hass: query),
        patch.object(m, "gateway_query", lambda _hass: gateway),
        patch.object(m, "read_current_run", lambda: dict(run)),
    ):
        monitor = await m.async_register_power_events(hass)
        assert await m.async_register_power_events(hass) is monitor
    await monitor.started
    if monitor.site_check is not None:
        await monitor.site_check
    return hass, monitor


async def websocket_read(hass) -> dict:
    responses = asyncio.Queue()

    def send(message):
        responses.put_nowait(
            json.loads(message) if isinstance(message, bytes | str) else message
        )

    options = (
        {"remote": None}
        if "remote" in ActiveConnection.__init__.__code__.co_varnames
        else {}
    )
    connection = ActiveConnection(
        logging.getLogger(__name__),
        hass,
        send,
        ADMIN,
        types.SimpleNamespace(id="test"),
        **options,
    )
    connection.async_handle({"id": 1, "type": "rexlite/power_events"})
    result = await asyncio.wait_for(responses.get(), 5)
    assert result["success"], result
    return result["result"]


async def main():
    with tempfile.TemporaryDirectory() as directory:
        engine = recorder_database(Path(directory, "home-assistant_v2.db"))
        storage = Path(directory, ".storage", m.DATA_KEY)

        with Session(engine) as session:
            runs = m.recorder_unclean_runs(session, at(-1))
            assert [(r["start"], r["end"], r["latest"]) for r in runs] == [
                (at(20), at(30), True),
                (at(0), at(10), False),
            ], runs
            # The event at 29.5 h is later than the last state write.
            assert runs[0]["lastAlive"] == at(29.5)
            assert runs[1]["lastAlive"] == at(9.5)
            assert [r["start"] for r in m.recorder_unclean_runs(session, at(5))] == [
                at(20)
            ]

        # First start with this version: the recorder's history is taken in.
        # The gateway has been up a minute: it lost power with the hub.
        hass, monitor = await start_hass(
            directory, engine, current_run(30, 29.9, "b1"), [60]
        )
        snapshot = await websocket_read(hass)
        assert snapshot["schemaVersion"] == 1
        assert [e["kind"] for e in snapshot["events"]] == ["power", "unclean"]
        assert snapshot["events"][0]["lostAt"] == m.to_iso(at(29.5))
        assert snapshot["events"][0]["restoredAt"] == m.to_iso(at(29.9))

        # Someone restarts from the HA UI: the stop is stamped with who asked.
        hass.bus.async_fire(
            EVENT_CALL_SERVICE,
            {"domain": "homeassistant", "service": "restart"},
            context=Context(user_id=ADMIN.id),
        )
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await hass.async_block_till_done()
        stored = json.loads(await hass.async_add_executor_job(storage.read_text))
        assert stored["data"]["run"]["stopBy"] == "Admin", stored["data"]["run"]

        hass, monitor = await start_hass(
            directory, engine, current_run(35, 29.9, "b1"), [60]
        )
        manual = monitor.events[0]
        assert (manual["kind"], manual["by"]) == ("manual", "Admin"), manual

        # That run dies without a stop and the host boots again, but the
        # gateway stayed up for a week: a reset, not a power cut.
        hass, monitor = await start_hass(
            directory, engine, current_run(41, 40.5, "b2"), [7 * 86400]
        )
        reset = monitor.events[0]
        assert (reset["kind"], reset["site"]) == ("reboot", "up"), reset
        assert reset["id"] == m.to_iso(at(35))
        assert reset["lostAt"] == m.to_iso(at(40))
        assert reset["restoredAt"] == m.to_iso(at(40.5))
        assert (await websocket_read(hass))["events"][0] == reset

        # A backup copies the record of the run making it; restoring that copy
        # (here on another host) reads as a restore, not as a cut.
        await monitor.backing_up(True)
        backup_copy = await hass.async_add_executor_job(storage.read_text)
        await monitor.backing_up(False)
        assert "backupAt" not in monitor.current
        await hass.async_add_executor_job(storage.write_text, backup_copy)
        hass, monitor = await start_hass(
            directory, engine, current_run(60, 59, "b9"), [60]
        )
        assert [e["kind"] for e in monitor.events] == [
            "restore",
            "reboot",
            "manual",
            "power",
            "unclean",
        ], monitor.events
    await production_paths()
    print(f"power events runtime check passed on Home Assistant {__version__}")


async def production_paths():
    """The recorder read and the UPnP calls exactly as the hub makes them."""
    import inspect

    from async_upnp_client.aiohttp import AiohttpSessionRequester
    from async_upnp_client.client_factory import UpnpFactory
    from async_upnp_client.profiles.igd import IgdDevice, StatusInfo
    from async_upnp_client.search import async_search
    from homeassistant import config_entries, loader
    from homeassistant.helpers import recorder as recorder_helper
    from homeassistant.setup import async_setup_component

    search = inspect.signature(async_search).parameters
    assert {"async_callback", "timeout", "search_target"} <= set(search), search
    assert "non_strict" in inspect.signature(UpnpFactory).parameters
    requester = inspect.signature(AiohttpSessionRequester).parameters
    assert {"with_sleep", "timeout"} <= set(requester), requester
    assert hasattr(IgdDevice, "async_get_status_info")
    assert "uptime" in StatusInfo._fields

    with tempfile.TemporaryDirectory() as directory:
        hass = HomeAssistant(directory)
        loader.async_setup(hass)
        hass.config.skip_pip = True
        recorder_helper.async_initialize_recorder(hass)
        hass.config_entries = config_entries.ConfigEntries(hass, {})
        await hass.config_entries.async_initialize()
        database = f"sqlite:///{directory}/home-assistant_v2.db"
        assert await async_setup_component(
            hass, "recorder", {"recorder": {"db_url": database, "commit_interval": 0}}
        )
        await hass.async_start()
        hass.states.async_set("sensor.alive", "1")
        await hass.async_block_till_done()
        await recorder_helper.get_instance(hass).async_block_till_done()
        query = m.recorder_query(hass)
        now = datetime.now(UTC)
        runs = await query(
            lambda s: m.recorder_unclean_runs(s, now - timedelta(days=1))
        )
        assert runs == [], runs
        alive = await query(
            lambda s: m.recorder_last_alive(
                s, now - timedelta(hours=1), now + timedelta(hours=1)
            )
        )
        assert alive is not None and abs((alive - now).total_seconds()) < 120, alive
        version, integrations = await m.installed_versions(hass)
        assert version == __version__ and isinstance(integrations, dict)
        await hass.async_stop(force=True)


if __name__ == "__main__":
    asyncio.run(main())
