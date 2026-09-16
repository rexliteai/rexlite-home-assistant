"""Protocol/measurement checks without router access or a Home Assistant install."""

import importlib.util
import unittest
from pathlib import Path

PATH = Path(__file__).parents[1] / "custom_components/rexlite/network_traffic.py"
spec = importlib.util.spec_from_file_location("traffic_sampler_test", PATH)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def channel(**changes):
    return m.validate_channel(
        {
            "id": "port1",
            "name": "Zyxel port 1",
            "provider": "zyxel",
            "scope": "interface",
            "mode": "counter",
            "unit": "B",
            "rx_entity": "sensor.rx",
            "tx_entity": "sensor.tx",
            "discontinuity_entity": "sensor.reset",
            "uptime_entity": "sensor.uptime",
            **changes,
        }
    )


def states(rx="100", tx="200", stamp=1000, reset="0", uptime=None):
    return {
        key: {"state": value, "timestamp": stamp, "attributes": {}}
        for key, value in {
            "sensor.rx": rx,
            "sensor.tx": tx,
            "sensor.reset": reset,
            "sensor.uptime": str(stamp * 100) if uptime is None else uptime,
        }.items()
    }


class TrafficTests(unittest.TestCase):
    def test_fortinet_source_preserves_interface_scope(self):
        sampler = m.TrafficSampler()
        config = channel(provider="fortinet")
        sampler.sample(config, states(), 1000)
        result = sampler.sample(config, states("1250100", "125200", 1010), 1010)
        self.assertEqual(result["provider"], "fortinet")
        self.assertEqual(result["scope"], "interface")
        self.assertEqual(result["rxBitsPerSecond"], 1_000_000)

    def test_counter_interval_and_duplicate_read(self):
        sampler = m.TrafficSampler()
        self.assertEqual(
            sampler.sample(channel(), states(), 1000)["status"], "warming_up"
        )
        result = sampler.sample(channel(), states("1250100", "125200", 1010), 1010)
        self.assertEqual(result["rxBitsPerSecond"], 1_000_000)
        self.assertEqual(result["txBitsPerSecond"], 100_000)
        self.assertEqual(
            sampler.sample(channel(), states("1250100", "125200", 1010), 1019), result
        )
        zero = sampler.sample(channel(), states("1250100", "125200", 1020), 1020)
        self.assertEqual(zero["rxBitsPerSecond"], 0)

    def test_reset_reboot_wrap_and_clock_regression_do_not_spike(self):
        for next_state in [
            states("1", "1", 1010),
            states("2000", "2000", 1010, reset="1"),
            states("2000", "2000", 1010, uptime="0"),
            states("2000", "2000", 999),
        ]:
            sampler = m.TrafficSampler()
            sampler.sample(channel(), states(), 1000)
            row = sampler.sample(channel(), next_state, 1010)
            self.assertEqual(row["status"], "warming_up")
            self.assertIsNone(row["rxBitsPerSecond"])

    def test_stale_missing_and_long_gaps_reset_baseline(self):
        sampler = m.TrafficSampler()
        sampler.sample(channel(), states(), 1000)
        self.assertEqual(sampler.sample(channel(), states(), 1121)["status"], "stale")
        self.assertEqual(
            sampler.sample(channel(), states(stamp=1130), 1130)["status"], "warming_up"
        )
        self.assertEqual(sampler.sample(channel(), {}, 1140)["status"], "unavailable")
        self.assertEqual(
            sampler.sample(channel(), states(stamp=1150), 1150)["status"], "warming_up"
        )

    def test_64bit_precision_and_missing_marker(self):
        sampler = m.TrafficSampler()
        base = 2**63
        sampler.sample(channel(), states(str(base), str(base)), 1000)
        row = sampler.sample(
            channel(), states(str(base + 1250), str(base + 125), 1010), 1010
        )
        self.assertEqual(row["rxBitsPerSecond"], 1000)
        self.assertEqual(row["txBitsPerSecond"], 100)
        self.assertIsNone(
            sampler.sample(channel(), states(stamp=1020, reset="unavailable"), 1020)[
                "rxBitsPerSecond"
            ]
        )

    def test_explicit_rate_units_and_attributes(self):
        config = channel(
            scope="device",
            device_id="tv",
            mode="rate",
            unit="kB/s",
            rx_entity="device_tracker.tv",
            tx_entity="device_tracker.tv",
            rx_attribute="down",
            tx_attribute="up",
        )
        data = {
            "device_tracker.tv": {
                "state": "home",
                "timestamp": 1000,
                "attributes": {"down": 125, "up": 12.5, "password": "do-not-export"},
            }
        }
        row = m.TrafficSampler().sample(config, data, 1000)
        self.assertEqual(row["rxBitsPerSecond"], 1_000_000)
        self.assertEqual(row["deviceId"], "tv")
        self.assertNotIn("do-not-export", str(row))
        data["device_tracker.tv"]["state"] = "not_home"
        self.assertIsNone(
            m.TrafficSampler().sample(config, data, 1000)["rxBitsPerSecond"]
        )

    def test_units_invalid_values_and_disabled_sources_fail_closed(self):
        for raw in ["NaN", "Infinity", "-1", "", str(2**64), True, float(2**63)]:
            row = m.TrafficSampler().sample(channel(), states(rx=raw), 1000)
            self.assertIsNone(row["rxBitsPerSecond"])
        config = channel(mode="rate", unit="Mbps")
        data = states(rx="1")
        data["sensor.rx"]["attributes"] = {"unit_of_measurement": "MB/s"}
        self.assertIsNone(
            m.TrafficSampler().sample(config, data, 1000)["rxBitsPerSecond"]
        )

    def test_configuration_scope_and_secret_rejection(self):
        for changes in [
            {"device_id": "tv"},
            {"scope": "device"},
            {"rx_entity": "switch.power"},
            {"tx_entity": "sensor.rx"},
            {"unit": "packets"},
            {"password": "secret"},
            {"discontinuity_entity": ""},
            {"uptime_entity": ""},
            {"id": 123},
            {"unit": ["Mbps"]},
            {"device_id": None},
        ]:
            with self.assertRaises(ValueError):
                channel(**changes)

    def test_channels_are_isolated_and_removable(self):
        sampler = m.TrafficSampler()
        sampler.sample(channel(), states(), 1000)
        sampler.sample(channel(id="other"), states(), 1000)
        sampler.forget("port1")
        self.assertEqual(
            sampler.sample(channel(), states(stamp=1010), 1010)["status"], "warming_up"
        )
        self.assertEqual(
            sampler.sample(channel(id="other"), states(stamp=1010), 1010)["status"],
            "available",
        )
