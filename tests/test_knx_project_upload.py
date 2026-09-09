import base64
import hashlib
import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "uploads",
    Path(__file__).parents[1] / "custom_components/rexlite/knx_project_upload.py",
)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.store = m.ProjectUploads()
        self.base = dict(uploadId="a" * 32, owner="operator")

    def tearDown(self):
        self.store.close()

    def call(self, action, **kw):
        return self.store.request(dict(self.base, action=action, **kw))

    def start(self, data):
        return self.call(
            "start",
            fileName="test.knxproj",
            size=len(data),
            projectFingerprint=hashlib.sha256(data).hexdigest(),
        )

    def test_retry_owner_offset_and_integrity(self):
        data = b"PK\x03\x04abcdefgh"
        self.start(data)
        first = base64.b64encode(data[:4]).decode()
        self.call("chunk", offset=0, data=first)
        self.assertEqual(self.call("chunk", offset=0, data=first)["offset"], 4)
        with self.assertRaisesRegex(ValueError, "retry_mismatch"):
            self.call("chunk", offset=0, data=base64.b64encode(b"xxxx").decode())
        with self.assertRaisesRegex(ValueError, "offset"):
            self.call("chunk", offset=5, data=first)
        with self.assertRaisesRegex(ValueError, "not_found"):
            self.store.request(dict(self.base, action="discard", owner="different"))
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.call("seal")
        self.call("chunk", offset=4, data=base64.b64encode(data[4:]).decode())
        self.assertTrue(self.call("seal")["sealed"])
        with self.store.consume("rexlite-" + self.base["uploadId"]) as path:
            self.assertEqual(path.read_bytes(), data)
            with self.assertRaisesRegex(ValueError, "processing"):
                self.call("discard")
        self.assertFalse(path.exists())

    def test_exact_100_mib_streamed_and_oversize_rejected(self):
        block = b"PK\x03\x04" + b"x" * (m.CHUNK_BYTES - 4)
        count = m.MAX_BYTES // len(block)
        digest = hashlib.sha256()
        for _ in range(count):
            digest.update(block)
        self.call(
            "start",
            fileName="large.knxproj",
            size=m.MAX_BYTES,
            projectFingerprint=digest.hexdigest(),
        )
        encoded = base64.b64encode(block).decode()
        for index in range(count):
            self.call("chunk", offset=index * len(block), data=encoded)
        result = self.call("seal")
        self.assertEqual(result["size"], 104857600)
        self.assertEqual(result["projectFingerprint"], digest.hexdigest())
        with self.assertRaisesRegex(ValueError, "too_large"):
            self.call(
                "start",
                fileName="large.knxproj",
                size=m.MAX_BYTES + 1,
                projectFingerprint=digest.hexdigest(),
            )

    def test_wrong_fingerprint_cannot_be_consumed(self):
        self.call("start", fileName="test.knxproj", size=4, projectFingerprint="0" * 64)
        self.call("chunk", offset=0, data="UEsDBA==")
        with self.assertRaisesRegex(ValueError, "fingerprint_mismatch"):
            self.call("seal")
        with (
            self.assertRaisesRegex(ValueError, "not_ready"),
            self.store.consume("rexlite-" + self.base["uploadId"]),
        ):
            pass


if __name__ == "__main__":
    unittest.main()
