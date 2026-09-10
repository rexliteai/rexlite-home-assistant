#!/usr/bin/env python3
"""Opt-in, reversible REXLiTE icon repair for the official HACS 2.0.5 bundle.

This maintenance command is never run by the integration. It changes only the
REXLiTE icon URL in a verified HACS frontend, embedding the existing PNG so the
store does not depend on the discontinued custom-brand CDN. Other brands and
all Home Assistant/Python behavior remain unchanged.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ORIGINAL = (
    b'e=>`https://brands.home-assistant.io/${e.brand?"brands/":""}'
    b'${e.useFallback?"_/":""}${e.domain}/'
    b'${e.darkOptimized?"dark_":""}${e.type}.png`'
)
ICON_HASH = "632e29eb55979a73b18be3ab363d8f456edfbb71edec830419458ec776ed5715"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_regular(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Expected a regular file: {path}")
    return path.read_bytes()


def atomic_write(path: Path, data: bytes) -> None:
    """Publish a complete file while retaining permissions and ownership."""
    original_stat = path.stat()
    fd, temporary = tempfile.mkstemp(prefix=".rexlite-icon-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fchmod(handle.fileno(), stat.S_IMODE(original_stat.st_mode))
            if hasattr(os, "fchown"):
                os.fchown(handle.fileno(), original_stat.st_uid, original_stat.st_gid)
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def repair(config: Path, *, apply: bool = False, restore: bool = False) -> int:
    config = config.resolve(strict=True)
    hacs = config / "custom_components/hacs"
    if (config / "custom_components").is_symlink() or hacs.is_symlink():
        raise ValueError("Refusing a symlinked custom_components or HACS directory")
    if json.loads(read_regular(hacs / "manifest.json"))["version"] != "2.0.5":
        raise ValueError("Only the official HACS 2.0.5 release is supported")
    frontend = hacs / "hacs_frontend"
    hashes = json.loads((ROOT / "scripts/hacs-2.0.5-brand-files.json").read_text())
    icon = read_regular(ROOT / "custom_components/rexlite/brand/icon@2x.png")
    if digest(icon) != ICON_HASH:
        raise ValueError("Original approved REXLiTE icon checksum mismatch")
    icon_url = b'"data:image/png;base64,' + base64.b64encode(icon) + b'"'
    backup = config / ".rexlite-hacs-icon-backup-2.0.5"
    changes: list[tuple[Path, Path, bytes, bytes]] = []

    # Check every target and its compressed copy before making any change.
    for relative, expected in hashes.items():
        path = frontend / relative
        for candidate in (
            frontend,
            path.parent,
            backup,
            backup / Path(relative).parent,
        ):
            if candidate.is_symlink():
                raise ValueError(f"Refusing symlink: {candidate}")
        current = read_regular(path)
        saved = backup / relative
        original = read_regular(saved) if saved.exists() else current
        variants = [
            ORIGINAL.replace(b"e=>", arg + b"=>").replace(b"${e.", b"${" + arg + b".")
            for arg in (b"e", b"i", b"t")
        ]
        matches = [value for value in variants if original.count(value) == 1]
        if digest(original) != expected or len(matches) != 1:
            raise ValueError(f"Unrecognized HACS bundle; no files changed: {relative}")
        expression = matches[0]
        arg = expression[:1]
        replacement = (
            arg
            + b"=>"
            + arg
            + b'.domain==="rexlite"&&'
            + arg
            + b'.type==="icon"?'
            + icon_url
            + b":"
            + expression[3:]
        )
        patched = original.replace(expression, replacement)
        if current not in (original, patched):
            raise ValueError(f"HACS file was modified by another tool: {relative}")
        if path.with_suffix(".js.br").exists():
            raise ValueError(f"Unexpected Brotli bundle: {relative}")
        target = original if restore else patched
        changes.append((path, saved, original, target))

        compressed = path.with_suffix(".js.gz")
        compressed_saved = saved.with_suffix(".js.gz")
        compressed_current = read_regular(compressed)
        original_gzip = (
            read_regular(compressed_saved)
            if compressed_saved.exists()
            else compressed_current
        )
        if gzip.decompress(original_gzip) != original:
            raise ValueError(f"Original gzip does not match JavaScript: {relative}")
        if gzip.decompress(compressed_current) not in (original, patched):
            raise ValueError(f"Gzip was modified by another tool: {relative}")
        target_gzip = original_gzip if restore else gzip.compress(patched, mtime=0)
        changes.append((compressed, compressed_saved, original_gzip, target_gzip))

    pending = [
        (path, saved, original, target)
        for path, saved, original, target in changes
        if read_regular(path) != target
    ]
    if not apply and not restore:
        print(
            f"Verified HACS 2.0.5: {len(pending)} files need repair. No files changed."
        )
        return len(pending)

    # Retain original bytes before writes. Interrupted runs can safely resume.
    for _, saved, original, _ in changes:
        saved.parent.mkdir(parents=True, exist_ok=True)
        if not saved.exists():
            with saved.open("xb") as handle:
                handle.write(original)
                handle.flush()
                os.fsync(handle.fileno())
    written: list[tuple[Path, bytes]] = []
    try:
        for path, _, _, target in pending:
            before = read_regular(path)
            atomic_write(path, target)
            written.append((path, before))
    except Exception:
        for path, before in reversed(written):
            atomic_write(path, before)
        raise
    print(
        f"{'Restored' if restore else 'Repaired'} {len(pending)} files. "
        "Fully refresh the HACS browser page. No Home Assistant restart is needed."
    )
    print(f"Original files retained at {backup}")
    return len(pending)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="Home Assistant config directory, e.g. /config",
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--apply", action="store_true")
    actions.add_argument("--restore", action="store_true")
    args = parser.parse_args()
    try:
        repair(args.config, apply=args.apply, restore=args.restore)
    except (OSError, ValueError, KeyError) as err:
        parser.exit(1, f"Repair stopped: {err}\n")


if __name__ == "__main__":
    main()
