#!/usr/bin/env bash
# Install a built wheel into a fresh venv, then run the example app and `rowx check` against it.
# Usage: smoke-wheel.sh dist/*.whl   (DATABASE_URL must point at a database with examples/app/schema.sql applied)
set -euo pipefail

if (($# != 1)); then
  echo "usage: $0 <wheel>" >&2
  exit 2
fi
if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "DATABASE_URL is not set" >&2
  exit 2
fi

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
wheel="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
venv="$(mktemp -d)"
trap 'rm -rf "$venv"' EXIT

"${PYTHON:-python3.14}" -m venv "$venv"
"$venv/bin/python" -m pip install --quiet "$wheel"

# run from a scratch directory so the source tree's src/rowx can't shadow the installed wheel
cd "$venv"
"$venv/bin/python" -c "import rowx, rowx._core"
"$venv/bin/python" "$root/examples/app/main.py"
"$venv/bin/rowx" check "$root/examples/app"
