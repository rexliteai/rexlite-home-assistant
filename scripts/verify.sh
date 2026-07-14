#!/usr/bin/env bash
set -Eeuo pipefail

readonly ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly PYTHON_BIN="${PYTHON_BIN:-python3}"
readonly CACHE_DIR="${TMPDIR:-/tmp}/rexlite-home-assistant-pycache"

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
