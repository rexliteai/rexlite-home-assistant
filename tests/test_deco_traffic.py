"""Deco state reports retain actual freshness and exclude unrelated state data."""

import importlib.util
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

PATH = Path(__file__).parents[1] / "custom_components/rexlite/deco_traffic.py"
spec = importlib.util.spec_from_file_location("deco_reports_test", PATH)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class DecoReportsTests(unittest.TestCase):
    def test_quiet_clients_keep_report_time_without_inventing_freshness(self):
        old = datetime(2026, 9, 17, tzinfo=UTC)
        report = old + timedelta(minutes=30)
        state = SimpleNamespace(
            state="home",
            last_updated=old,
            last_reported=report,
            attributes={
                "device_type": "client",
                "down_kilobytes_per_s": 0,
                "up_kilobytes_per_s": 0,
                "password": "PRIVATE",
            },
        )
        hass = SimpleNamespace(states=SimpleNamespace(get=lambda _: state))
        registry = SimpleNamespace(
            entities={
                "client": SimpleNamespace(
                    platform="tplink_deco", entity_id="device_tracker.client"
                ),
                "other": SimpleNamespace(
                    platform="other", entity_id="device_tracker.other"
                ),
                "sensor": SimpleNamespace(
                    platform="tplink_deco", entity_id="sensor.other"
                ),
            }
        )
        rows = m.deco_state_reports(hass, registry)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["last_reported"], report.isoformat())
        self.assertEqual(rows[0]["attributes"]["down_kilobytes_per_s"], 0)
        self.assertNotIn("password", rows[0]["attributes"])
        self.assertEqual(m.deco_state_reports(hass, registry), rows)
        state.state = "unavailable"
        self.assertEqual(
            m.deco_state_reports(hass, registry)[0]["state"], "unavailable"
        )
