#!/usr/bin/env bash
set -Eeuo pipefail

readonly ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly CACHE_DIR="${TMPDIR:-/tmp}/rexlite-home-assistant-pycache"

if [[ -z "${PYTHON_BIN:-}" ]]; then
  for candidate in python3.14 python3.13 python3.12 python3; do
    if command -v "${candidate}" >/dev/null 2>&1; then
      PYTHON_BIN="${candidate}"
      break
    fi
  done
fi

if [[ -z "${PYTHON_BIN:-}" ]]; then
  echo "Python 3.12 or newer is required for verification" >&2
  exit 1
fi

readonly PYTHON_BIN

if ! "${PYTHON_BIN}" - <<'PY'
import sys

if sys.version_info < (3, 12):
    raise SystemExit(1)
PY
then
  echo "${PYTHON_BIN} must be Python 3.12 or newer" >&2
  exit 1
fi

cd "${ROOT_DIR}"

PYTHONPYCACHEPREFIX="${CACHE_DIR}" "${PYTHON_BIN}" -m compileall -q -f custom_components tests
PYTHONPYCACHEPREFIX="${CACHE_DIR}" "${PYTHON_BIN}" -m unittest discover -s tests -p 'test_*.py'

"${PYTHON_BIN}" - <<'PY'
import json
import struct
from pathlib import Path

paths = [Path("hacs.json"), *Path("custom_components/rexlite").rglob("*.json")]
for path in paths:
    with path.open(encoding="utf-8") as handle:
        json.load(handle)
print(f"Validated {len(paths)} JSON files")

brand_directory = Path("custom_components/rexlite/brand")
brand_assets = {
    "icon.png": (128, 128, True),
    "logo.png": (128, 64, False),
}
for filename, (minimum_width, minimum_height, must_be_square) in brand_assets.items():
    path = brand_directory / filename
    header = path.read_bytes()[:24]
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        raise SystemExit(f"{path} is not a valid PNG image")
    width, height = struct.unpack(">II", header[16:24])
    if width < minimum_width or height < minimum_height:
        raise SystemExit(
            f"{path} must be at least {minimum_width}x{minimum_height} pixels"
        )
    if must_be_square and width != height:
        raise SystemExit(f"{path} must be square")
print(f"Validated {len(brand_assets)} local brand assets")
PY

if ! "${PYTHON_BIN}" -m ruff --version >/dev/null 2>&1; then
  echo "Ruff is required for verification; install it with: ${PYTHON_BIN} -m pip install ruff" >&2
  exit 1
fi

"${PYTHON_BIN}" -m ruff check custom_components tests
"${PYTHON_BIN}" -m ruff format --check custom_components tests

git diff --check
echo "REXLiTE HACS integration verification passed"
