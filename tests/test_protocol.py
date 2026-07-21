"""Protocol regression tests that do not require Home Assistant."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

MODULE_PATH = (
    Path(__file__).parents[1] / "custom_components" / "rexlite" / "protocol.py"
)
SPEC = importlib.util.spec_from_file_location("rexlite_protocol", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Unable to load REXLiTE protocol module")
protocol = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = protocol
SPEC.loader.exec_module(protocol)


class ProtocolTests(unittest.TestCase):
    """Protect compatibility and security boundaries in the tunnel protocol."""

    def test_envelope_round_trip(self) -> None:
        raw = protocol.make_envelope("heartbeat", "request-1", {"status": "ok"})
        envelope = protocol.parse_envelope(raw)
        self.assertEqual(envelope.message_type, "heartbeat")
        self.assertEqual(envelope.request_id, "request-1")
        self.assertEqual(envelope.payload, {"status": "ok"})

    def test_bytes_use_go_compatible_base64(self) -> None:
        encoded = protocol.encode_bytes(b"REXLiTE\x00")
        self.assertEqual(encoded, "UkVYTGlURQA=")
        self.assertEqual(protocol.decode_bytes(encoded, maximum=32), b"REXLiTE\x00")

    def test_decode_bytes_rejects_oversized_or_invalid_values(self) -> None:
        with self.assertRaises(protocol.ProtocolError):
            protocol.decode_bytes("not base64", maximum=64)
        with self.assertRaises(protocol.ProtocolError):
            protocol.decode_bytes(protocol.encode_bytes(b"12345"), maximum=4)

    def test_agent_url_preserves_gateway_query_and_adds_metadata(self) -> None:
        result = protocol.agent_websocket_url(
            "wss://gateway.example/ws/agent?region=tw",
            agent_id="hub-1",
            version="0.1.0",
            remote_admin_enabled=False,
        )
        self.assertIn("region=tw", result)
        self.assertIn("agent_id=hub-1", result)
        self.assertIn("meta_remote_access_mode=health_only", result)

    def test_local_request_is_pinned_to_configured_origin(self) -> None:
        result = protocol.local_request_url(
            "http://127.0.0.1:8123/base", "/api/config", "a=1"
        )
        self.assertEqual(result, "http://127.0.0.1:8123/base/api/config?a=1")
        with self.assertRaises(protocol.ProtocolError):
            protocol.local_request_url(
                "http://127.0.0.1:8123", "https://attacker.invalid/"
            )

    def test_http_response_body_plan_finishes_keep_alive_responses(self) -> None:
        self.assertEqual(
            protocol.http_response_body_plan("GET", 200, {"Content-Length": ["128"]}),
            protocol.HttpResponseBodyPlan("fixed", 128),
        )
        self.assertEqual(
            protocol.http_response_body_plan("HEAD", 405, {"Content-Length": ["23"]}),
            protocol.HttpResponseBodyPlan("none"),
        )
        self.assertEqual(
            protocol.http_response_body_plan(
                "GET", 200, {"Transfer-Encoding": ["chunked"]}
            ),
            protocol.HttpResponseBodyPlan("chunked"),
        )
        self.assertEqual(
            protocol.http_response_body_plan(
                "GET", 101, {"Connection": ["upgrade"], "Upgrade": ["websocket"]}
            ),
            protocol.HttpResponseBodyPlan("close"),
        )

    def test_http_response_body_plan_rejects_ambiguous_framing(self) -> None:
        with self.assertRaises(protocol.ProtocolError):
            protocol.http_response_body_plan(
                "GET", 200, {"Content-Length": ["12", "13"]}
            )
        with self.assertRaises(protocol.ProtocolError):
            protocol.http_response_body_plan(
                "GET", 200, {"Transfer-Encoding": ["gzip"]}
            )
        with self.assertRaises(protocol.ProtocolError):
            protocol.http_response_body_plan(
                "GET", 200, {"Transfer-Encoding": ["gzip, chunked"]}
            )

    def test_urls_reject_credentials_and_unsafe_schemes(self) -> None:
        with self.assertRaises(protocol.ProtocolError):
            protocol.validate_gateway_url("https://gateway.example/ws/agent")
        with self.assertRaises(protocol.ProtocolError):
            protocol.validate_gateway_url("wss://user:secret@gateway.example/ws/agent")
        with self.assertRaises(protocol.ProtocolError):
            protocol.validate_local_url("file:///config/configuration.yaml")


if __name__ == "__main__":
    unittest.main()
