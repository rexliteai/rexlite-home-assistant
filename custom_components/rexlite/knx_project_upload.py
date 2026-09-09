"""Bounded, retry-safe ETS staging over small authenticated WebSocket messages."""

from __future__ import annotations

import base64
import hashlib
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

DATA_KEY = "rexlite_knx_uploads"
MAX_BYTES = 100 * 1024 * 1024
CHUNK_BYTES = 512 * 1024
TTL = 30 * 60


class ProjectUploads:
    def __init__(self):
        self.directory = tempfile.TemporaryDirectory(prefix="rexlite-ets-")
        self.sessions = {}
        self.lock = threading.Lock()

    def close(self):
        with self.lock:
            self.sessions.clear()
            self.directory.cleanup()

    def _remove(self, key):
        self.sessions.pop(key, None)
        (Path(self.directory.name) / key).unlink(missing_ok=True)

    def request(self, msg):
        with self.lock:
            return self._request(msg)

    def _request(self, msg):
        now = time.monotonic()
        for key, session in list(self.sessions.items()):
            if now - session["touched"] > TTL and not session.get("consuming"):
                self._remove(key)
        key = msg.get("uploadId", "")
        if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{32}", key):
            raise ValueError("invalid_upload_id")
        action = msg.get("action")
        owner = msg.get("owner", "")
        if not isinstance(owner, str) or not owner or len(owner) > 256:
            raise ValueError("invalid_upload_owner")
        session = self.sessions.get(key)
        path = Path(self.directory.name) / key
        if action == "start":
            size, name, fingerprint = (
                msg.get("size"),
                msg.get("fileName"),
                msg.get("projectFingerprint"),
            )
            if type(size) is not int or not 4 <= size <= MAX_BYTES:
                raise ValueError("project_file_too_large")
            if (
                not isinstance(name, str)
                or len(name) > 255
                or not re.fullmatch(
                    r"[^/\\\x00-\x1f]+\.(knxproj|knxprojarchive)", name, re.I
                )
            ):
                raise ValueError("invalid_project_filename")
            if not isinstance(fingerprint, str) or not re.fullmatch(
                r"[a-f0-9]{64}", fingerprint
            ):
                raise ValueError("invalid_project_fingerprint")
            metadata = dict(
                owner=owner, size=size, fileName=name, projectFingerprint=fingerprint
            )
            if session:
                if any(session[k] != v for k, v in metadata.items()):
                    raise ValueError("upload_metadata_mismatch")
            else:
                if len(self.sessions) >= 2:
                    raise ValueError("upload_capacity_reached")
                with path.open("xb"):
                    path.chmod(0o600)
                session = self.sessions[key] = dict(
                    **metadata, offset=0, touched=now, sealed=False
                )
        if session is None or session["owner"] != owner:
            raise ValueError("upload_not_found")
        if session.get("consuming"):
            raise ValueError("upload_processing")
        session["touched"] = now
        if action == "chunk":
            data = msg.get("data", "")
            if not isinstance(data, str) or len(data) > 699052:
                raise ValueError("invalid_upload_chunk")
            try:
                chunk = base64.b64decode(data, validate=True)
            except Exception as err:
                raise ValueError("invalid_upload_chunk") from err
            offset = msg.get("offset")
            if (
                type(offset) is not int
                or offset < 0
                or not 0 < len(chunk) <= CHUNK_BYTES
                or offset + len(chunk) > session["size"]
            ):
                raise ValueError("invalid_upload_offset")
            with path.open("r+b") as stream:
                stream.seek(offset)
                if offset < session["offset"]:
                    if (
                        offset + len(chunk) > session["offset"]
                        or stream.read(len(chunk)) != chunk
                    ):
                        raise ValueError("upload_retry_mismatch")
                elif offset == session["offset"] and not session["sealed"]:
                    stream.write(chunk)
                    session["offset"] += len(chunk)
                else:
                    raise ValueError("invalid_upload_offset")
        elif action == "seal":
            if session["offset"] != session["size"]:
                raise ValueError("upload_incomplete")
            with path.open("rb") as stream:
                if stream.read(4) not in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
                    raise ValueError("invalid_project_archive")
                stream.seek(0)
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            if actual != session["projectFingerprint"]:
                raise ValueError("project_fingerprint_mismatch")
            session["sealed"] = True
        elif action == "discard":
            self._remove(key)
            return {"discarded": True}
        elif action != "start":
            raise ValueError("invalid_upload_action")
        return {
            k: session[k]
            for k in ("offset", "size", "fileName", "projectFingerprint", "sealed")
        }

    @contextmanager
    def consume(self, file_id):
        key = file_id.removeprefix("rexlite-")
        with self.lock:
            session = self.sessions.get(key)
            if not session or not session["sealed"] or session.get("consuming"):
                raise ValueError("upload_not_ready")
            session["consuming"] = True
        try:
            yield Path(self.directory.name) / key
        finally:
            with self.lock:
                self._remove(key)
