"""跳電紀錄 checks without a Home Assistant install or a real host."""

import asyncio
import importlib.util
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

PATH = Path(__file__).parents[1] / "custom_components/rexlite/power_events.py"
spec = importlib.util.spec_from_file_location("power_events_test", PATH)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

T0 = datetime(2026, 11, 16, 8, 0, tzinfo=UTC)
REXLITE_56 = {"rexlite": {"name": "REXLiTE AI", "version": "0.1.56"}}
REXLITE_57 = {"rexlite": {"name": "REXLiTE AI", "version": "0.1.57"}}


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def run(started, boot, boot_id, stopping=None, **extra):
    return {
        "startedAt": m.to_iso(at(started)),
        "hostBootAt": m.to_iso(at(boot)),
        "bootId": boot_id,
        "stoppingAt": m.to_iso(at(stopping)) if stopping is not None else None,
        "haVersion": "2026.9.1",
        "integrations": REXLITE_56,
        **extra,
    }


class Store:
    def __init__(self, data=None, fail_load=False):
        self.data, self.fail_load = data, fail_load

    async def async_load(self):
        if self.fail_load:
            raise ValueError("corrupt")
        return self.data

    async def async_save(self, data):
        self.data = {
            "run": dict(data["run"]),
            "events": [dict(e) for e in data["events"]],
        }


def query(result):
    calls = []

    async def run_query(job):
        calls.append(job)
        return result

    run_query.calls = calls
    return run_query


def gateways(*answers):
    """Each call answers the next uptime list; the last one repeats."""
    calls = []

    async def ask():
        calls.append(1)
        return list(answers[min(len(calls), len(answers)) - 1])

    ask.calls = calls
    return ask


def monitor(store, current, recorder=None, gateway=None, **options):
    scheduled, sleeps = [], []

    async def sleep(seconds):
        sleeps.append(seconds)

    events = m.PowerEvents(
        store,
        current,
        recorder or query(None),
        gateway,
        scheduled.append,
        sleep,
        **options,
    )
    events.scheduled, events.sleeps = scheduled, sleeps
    return events


def start(events):
    async def go():
        await events.start()
        for job in events.scheduled:
            await job

    asyncio.run(go())
    return events.store.data["events"]


class ProcTests(unittest.TestCase):
    def test_reads_host_boot_and_process_start(self):
        boot = m.host_boot("cpu  1 2 3\nbtime 1763280000\nprocesses 9\n")
        self.assertEqual(boot, datetime.fromtimestamp(1763280000, UTC))
        # A command name with spaces and parentheses must not shift the fields.
        stat = "42 (python3 (ha) x) S " + " ".join(["0"] * 18) + " 12345 0 0"
        self.assertEqual(
            m.process_start(stat, boot, 100), boot + timedelta(seconds=123.45)
        )
        self.assertIsNone(m.host_boot("cpu 1 2\n"))
        self.assertIsNone(m.process_start("garbage", boot, 100))

    def test_current_run_from_proc_files(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            (proc / "self").mkdir()
            (proc / "sys/kernel/random").mkdir(parents=True)
            (proc / "stat").write_text("btime 1763280000\n")
            (proc / "self/stat").write_text(
                "7 (python3) S " + " ".join(["0"] * 18) + " 6000 0"
            )
            (proc / "sys/kernel/random/boot_id").write_text("abc-123\n")
            current = m.read_current_run(proc)
        boot = datetime.fromtimestamp(1763280000, UTC)
        self.assertEqual(current["hostBootAt"], m.to_iso(boot))
        self.assertEqual(current["bootId"], "abc-123")
        self.assertIsNone(current["stoppingAt"])
        ticks = m.os.sysconf("SC_CLK_TCK")
        self.assertEqual(
            current["startedAt"], m.to_iso(boot + timedelta(seconds=6000 / ticks))
        )

    def test_missing_proc_still_identifies_the_run(self):
        with tempfile.TemporaryDirectory() as directory:
            current = m.read_current_run(Path(directory))
        self.assertIsNone(current["hostBootAt"])
        self.assertIsNone(current["bootId"])
        self.assertIsNotNone(m.from_iso(current["startedAt"]))


class ClassificationTests(unittest.TestCase):
    def test_planned_restarts_say_why(self):
        manual = m.explain_previous(
            run(0, -1, "a", stopping=60, stopBy="Nick"), run(62, -1, "a"), None
        )
        self.assertEqual((manual["kind"], manual["by"]), (m.MANUAL, "Nick"))
        self.assertEqual(manual["lostAt"], m.to_iso(at(60)))
        self.assertEqual(manual["restoredAt"], m.to_iso(at(62)))

        update = m.explain_previous(
            run(0, -1, "a", stopping=60, stopBy="@system"),
            run(62, -1, "a", integrations=REXLITE_57),
            None,
        )
        self.assertEqual(update["kind"], m.UPDATE)
        self.assertEqual(update["changes"], ["REXLiTE AI|0.1.56|0.1.57"])
        self.assertEqual(update["by"], "@system")

        host = m.explain_previous(run(0, -1, "a", stopping=60), run(65, 63, "b"), None)
        self.assertEqual(
            (host["kind"], host["restoredAt"]), (m.HOST_REBOOT, m.to_iso(at(63)))
        )
        plain = m.explain_previous(run(0, -1, "a", stopping=60), run(61, -1, "a"), None)
        self.assertEqual(plain["kind"], m.RESTART)
        self.assertNotIn("by", plain)

    def test_version_changes(self):
        before = {
            "haVersion": "2026.8.0",
            "integrations": {
                **REXLITE_56,
                "frigate": {"name": "Frigate", "version": "5.1"},
            },
        }
        after = {
            "haVersion": "2026.9.1",
            "integrations": {
                **REXLITE_57,
                "hacs": {"name": "HACS", "version": "2.0.5"},
            },
        }
        self.assertEqual(
            m.version_changes(before, after),
            [
                "Home Assistant|2026.8.0|2026.9.1",
                "Frigate|5.1|",
                "HACS||2.0.5",
                "REXLiTE AI|0.1.56|0.1.57",
            ],
        )
        # A run from before versions were kept is no update.
        self.assertEqual(m.version_changes({}, after), [])

    def test_hard_reboot_waits_for_the_site_check(self):
        event = m.explain_previous(run(0, -1, "a"), run(70, 68, "b"), at(42))
        self.assertEqual((event["kind"], event["site"]), (m.REBOOT, m.CHECKING))
        self.assertEqual(event["id"], m.to_iso(at(0)))
        self.assertEqual(event["lostAt"], m.to_iso(at(42)))
        self.assertEqual(event["restoredAt"], m.to_iso(at(68)))
        self.assertEqual(event["startedAt"], m.to_iso(at(70)))

    def test_same_boot_without_stop_is_a_crash(self):
        event = m.explain_previous(run(0, -1, "a"), run(5, -1, "a"), at(4))
        self.assertEqual(event["kind"], m.CRASH)
        self.assertEqual(event["restoredAt"], m.to_iso(at(5)))
        self.assertNotIn("site", event)

    def test_without_boot_id_the_boot_time_decides(self):
        previous, current = run(0, -1, ""), run(70, 68, "")
        self.assertEqual(m.explain_previous(previous, current, None)["kind"], m.REBOOT)
        current = run(70, -1, "")
        self.assertEqual(m.explain_previous(previous, current, None)["kind"], m.CRASH)
        current["hostBootAt"] = None
        self.assertEqual(m.explain_previous(previous, current, None)["kind"], m.UNCLEAN)

    def test_activity_outside_the_run_is_dropped(self):
        previous, current = run(0, -1, "a"), run(70, 68, "b")
        self.assertIsNone(m.explain_previous(previous, current, at(-5))["lostAt"])
        self.assertIsNone(m.explain_previous(previous, current, at(69))["lostAt"])

    def test_same_run_or_garbage_is_ignored(self):
        current = run(0, -1, "a")
        self.assertIsNone(m.explain_previous(dict(current), current, None))
        self.assertIsNone(m.explain_previous({"startedAt": "x"}, current, None))
        self.assertIsNone(m.explain_previous(None, current, None))

    def test_record_from_a_restored_backup(self):
        event = m.explain_previous(
            run(0, -1, "a", backupAt=m.to_iso(at(30))), run(5000, 4990, "x"), at(40)
        )
        self.assertEqual(event["kind"], m.RESTORE)
        self.assertIsNone(event["lostAt"])
        self.assertEqual(event["restoredAt"], m.to_iso(at(5000)))

    def test_backfill_classifies_only_the_latest_run(self):
        runs = [
            {"start": at(100), "end": at(130), "lastAlive": at(110), "latest": True},
            {"start": at(0), "end": at(50), "lastAlive": at(40), "latest": False},
        ]
        latest, older = m.backfilled_events(runs, run(130, 125, "boot-c"))
        self.assertEqual((latest["kind"], latest["site"]), (m.REBOOT, m.CHECKING))
        self.assertEqual(latest["restoredAt"], m.to_iso(at(125)))
        self.assertEqual(older["kind"], m.UNCLEAN)
        self.assertEqual(older["restoredAt"], m.to_iso(at(50)))
        self.assertEqual(older["lostAt"], m.to_iso(at(40)))
        crash = m.backfilled_events(runs[:1], run(130, 90, "boot-c"))[0]
        self.assertEqual(
            (crash["kind"], crash["restoredAt"]), (m.CRASH, m.to_iso(at(130)))
        )

    def test_site_state_needs_the_gateway_down_too(self):
        now = at(100)
        # The gateway came up 5 minutes ago, after the hub's last write at 40.
        self.assertEqual(m.site_state([300], now, at(40), at(0)), m.POWER)
        # Up for a week: only the hub went down.
        self.assertEqual(m.site_state([604800], now, at(40), at(0)), m.SITE_UP)
        self.assertEqual(m.site_state([], now, at(40), at(0)), m.SITE_UNKNOWN)
        self.assertEqual(m.site_state([604800], now, None, at(0)), m.SITE_UP)
        self.assertEqual(m.site_state([300], now, None, at(0)), m.SITE_UNKNOWN)

    def test_only_lan_gateways_are_asked(self):
        self.assertTrue(m.lan_location("http://192.168.68.1:1900/igd.xml"))
        self.assertTrue(m.lan_location("http://10.0.0.1/rootDesc.xml"))
        self.assertFalse(m.lan_location("http://8.8.8.8/igd.xml"))
        self.assertFalse(m.lan_location("http://router.example/igd.xml"))

    def test_merge_keeps_newest_unique_valid_records(self):
        old = {"id": "2026-11-01T00:00:00+00:00", "kind": "power", "lostAt": None}
        new = {"id": old["id"], "kind": "crash", "restoredAt": m.to_iso(at(1))}
        other = {"id": m.to_iso(at(0)), "kind": "power", "lostAt": m.to_iso(at(9))}
        bad = {"id": "", "kind": "power"}
        merged = m.merge_events([new, bad], [old, other])
        self.assertEqual([e["kind"] for e in merged], ["power", "crash"])
        many = [{"id": m.to_iso(at(i)), "kind": "crash"} for i in range(250)]
        merged = m.merge_events(many, [])
        self.assertEqual(len(merged), m.MAX_EVENTS)
        self.assertEqual(merged[0]["id"], m.to_iso(at(249)))


class MonitorTests(unittest.TestCase):
    def test_power_cut_takes_the_gateway_down_too(self):
        store = Store({"run": run(0, -1, "a"), "events": []})
        watcher = monitor(store, run(90, 88, "b"), query(at(50)), gateways([60]))
        events = start(watcher)
        self.assertEqual(events[0]["kind"], m.POWER)
        self.assertNotIn("site", events[0])
        self.assertEqual(events[0]["lostAt"], m.to_iso(at(50)))
        self.assertEqual(events[0]["restoredAt"], m.to_iso(at(88)))

    def test_hard_reset_with_the_gateway_up_is_no_power_cut(self):
        store = Store({"run": run(0, -1, "a"), "events": []})
        uptime = int((datetime.now(UTC) - at(-600)).total_seconds())
        watcher = monitor(store, run(90, 88, "b"), query(at(50)), gateways([uptime]))
        events = start(watcher)
        self.assertEqual((events[0]["kind"], events[0]["site"]), (m.REBOOT, m.SITE_UP))

    def test_gateway_still_booting_is_asked_again(self):
        store = Store({"run": run(0, -1, "a"), "events": []})
        ask = gateways([], [], [45])
        watcher = monitor(store, run(90, 88, "b"), query(at(50)), ask)
        events = start(watcher)
        self.assertEqual(events[0]["kind"], m.POWER)
        self.assertEqual((len(ask.calls), watcher.sleeps), (3, [m.SITE_RETRY] * 2))

    def test_no_gateway_answer_leaves_it_unknown(self):
        store = Store({"run": run(0, -1, "a"), "events": []})
        ask = gateways([])
        watcher = monitor(store, run(90, 88, "b"), query(at(50)), ask)
        events = start(watcher)
        self.assertEqual(
            (events[0]["kind"], events[0]["site"]), (m.REBOOT, m.SITE_UNKNOWN)
        )
        self.assertEqual(len(ask.calls), m.SITE_ATTEMPTS)
        self.assertEqual(len(watcher.sleeps), m.SITE_ATTEMPTS - 1)

    def test_restart_records_who_asked(self):
        async def name(user_id):
            return {"u1": "Nick"}[user_id]

        store = Store()
        first = monitor(store, run(0, -1, "a"), user_name=name)
        start(first)
        first.stop_requested("u1")
        asyncio.run(first.stopping())
        self.assertEqual(store.data["run"]["stopBy"], "Nick")
        events = start(monitor(store, run(5, -1, "a")))
        self.assertEqual((events[0]["kind"], events[0]["by"]), (m.MANUAL, "Nick"))

        # Without a name lookup a raw user id is never kept.
        anonymous = monitor(store, run(10, -1, "a"))
        start(anonymous)
        anonymous.stop_requested("u1")
        asyncio.run(anonymous.stopping())
        self.assertEqual(store.data["run"]["stopBy"], "@user")

    def test_stop_without_a_call_is_a_system_restart(self):
        store = Store()
        first = monitor(store, run(0, -1, "a"))
        start(first)
        asyncio.run(first.stopping())
        self.assertNotIn("stopBy", store.data["run"])
        events = start(monitor(store, run(5, -1, "a"), query(at(1))))
        self.assertEqual(events[0]["kind"], m.RESTART)

    def test_first_start_backfills_from_the_recorder(self):
        runs = [{"start": at(0), "end": at(30), "lastAlive": at(20), "latest": True}]
        watcher = monitor(Store(), run(30, 28, "b"), query(runs), gateways([60]))
        self.assertEqual(start(watcher)[0]["kind"], m.POWER)

    def test_check_cut_short_by_a_restart_becomes_unknown(self):
        checking = {"id": m.to_iso(at(0)), "kind": "reboot", "site": "checking"}
        store = Store({"run": run(5, -1, "a", stopping=9), "events": [checking]})
        events = start(monitor(store, run(10, -1, "a")))
        self.assertEqual(events[-1]["site"], m.SITE_UNKNOWN)

    def test_restored_backup_is_no_power_cut(self):
        store = Store()
        first = monitor(store, run(0, -1, "a"))
        start(first)
        asyncio.run(first.backing_up(True))
        backup = {"run": dict(store.data["run"]), "events": []}
        self.assertIsNotNone(backup["run"]["backupAt"])
        asyncio.run(first.backing_up(False))
        self.assertNotIn("backupAt", store.data["run"])
        # Restored on another host, days later.
        restored = Store(backup)
        alive = query(at(10))
        watcher = monitor(restored, run(5000, 4990, "other"), alive, gateways([60]))
        self.assertEqual([e["kind"] for e in start(watcher)], [m.RESTORE])
        self.assertEqual(alive.calls, [])

    def test_unreadable_history_starts_over(self):
        backfill = query([{"start": at(0), "end": at(1), "latest": True}])
        events = start(monitor(Store(fail_load=True), run(2, -1, "a"), backfill))
        self.assertEqual(events[0]["kind"], m.CRASH)

    def test_recorder_unavailable_still_records(self):
        store = Store({"run": run(0, -1, "a"), "events": []})
        watcher = monitor(store, run(60, 58, "b"), query(None), gateways([30]))
        events = start(watcher)
        self.assertEqual(events[0]["kind"], m.REBOOT)
        self.assertIsNone(events[0]["lostAt"])
        # Without the hub's last write, a fresh gateway proves nothing.
        self.assertEqual(events[0]["site"], m.SITE_UNKNOWN)


if __name__ == "__main__":
    unittest.main()
