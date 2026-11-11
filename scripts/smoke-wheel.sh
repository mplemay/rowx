#!/usr/bin/env bash
# Install the built distributions into a fresh venv, then run the example and checker.
# Usage: smoke-wheel.sh dist/   (DATABASE_URL must name a database with examples/app/schema.sql applied)
set -euo pipefail

if (($# != 1)); then
  echo "usage: $0 <distribution-directory>" >&2
  exit 2
fi
if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "DATABASE_URL is not set" >&2
  exit 2
fi

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
distributions="$(cd "$1" && pwd)"
rowx_wheels=("$distributions"/rowx-*.whl)
venv="$(mktemp -d)"
trap 'rm -rf "$venv"' EXIT

uv venv --python "${PYTHON:-python3}" "$venv"
uv pip install --refresh --python "$venv/bin/python" "${rowx_wheels[0]}[postgres]" "$distributions"/rowx_core-*.whl "$distributions"/rowx_postgres-*.whl

# run from a scratch directory so the source tree's src/rowx can't shadow the installed wheel
cd "$venv"
"$venv/bin/python" -c "import rowx, rowx_postgres, rowx_postgres._core"
"$venv/bin/python" -c "import importlib.resources as r; assert r.files('rowx_postgres').joinpath('_core.pyi').is_file()"
"$venv/bin/python" "$root/examples/app/main.py"
"$venv/bin/rowx" check "$root/examples/app"
