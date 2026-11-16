#!/usr/bin/env bash
# Install the rowx-embedded wheel in a clean venv with only rowx-core and rowx-postgres (no rowx CLI), then start an
# embedded instance and run SELECT 1 over its raw socket DSN. Usage: smoke-embedded-wheel.sh <dir with the three wheels>
set -euo pipefail

if [[ $# != 1 ]]; then
  echo "usage: $0 <wheel directory>" >&2
  exit 2
fi

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
wheels="$(cd "$1" && pwd)"
for name in rowx_embedded rowx_core rowx_postgres; do
  if [[ -z $(find "$wheels" -maxdepth 1 -name "${name}-*.whl" -print -quit) ]]; then
    echo "::error::No ${name} wheel in ${wheels}."
    exit 1
  fi
done

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

uv venv --python 3.14 "$work/venv"
# --no-index: only the wheels under test (and their pinned siblings) may satisfy the requirements
uv pip install --python "$work/venv/bin/python" --no-index --find-links "$wheels" rowx-embedded rowx-core rowx-postgres

# run from an empty directory so a checkout cannot shadow an installed package
cd "$work"
"$work/venv/bin/python" -I "$root/scripts/smoke-embedded-wheel.py"
