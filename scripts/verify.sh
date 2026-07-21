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
from pathlib import Path

paths = [Path("hacs.json"), *Path("custom_components/rexlite").rglob("*.json")]
for path in paths:
    with path.open(encoding="utf-8") as handle:
        json.load(handle)
print(f"Validated {len(paths)} JSON files")
PY

if command -v ruff >/dev/null 2>&1; then
  ruff check custom_components tests
  ruff format --check custom_components tests
fi

git diff --check
echo "REXLiTE HACS integration verification passed"
