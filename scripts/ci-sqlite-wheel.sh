#!/usr/bin/env bash
# Build the rowx-sqlite wheel (bundled SQLite C source), plus rowx and rowx-core, then install `rowx[sqlite]` from only
# those wheels into a clean environment and run the file-database smoke test. Runs on Linux, macOS and Windows (bash).
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work="$(mktemp -d)"
# native uv needs a Windows path (C:/...) rather than an MSYS one (/tmp/...) when a path is embedded in a requirement
if command -v cygpath >/dev/null; then
  work="$(cygpath -m "$work")"
fi
trap 'rm -rf "$work"' EXIT
wheels="$work/wheels"
mkdir -p "$wheels" "$work/empty"

uvx --from "maturin>=1.0,<2.0" maturin build --release --locked --manifest-path "$root/packages/rowx-sqlite/Cargo.toml" --interpreter "$(uv python find 3.14)" --out "$wheels"
uv build --package rowx --wheel --out-dir "$wheels"
uv build --package rowx-core --wheel --out-dir "$wheels"
uv run --no-project --python 3.14 python "$root/scripts/validate-distributions.py" --contents-only --directory "$wheels"

# run from an empty directory so a checkout cannot shadow an installed package; the three wheels are named explicitly so
# the install cannot resolve a sibling from PyPI (only pathspec, the CLI's own dependency, comes from the index)
rowx_wheels=("$wheels"/rowx-*.whl)
core_wheels=("$wheels"/rowx_core-*.whl)
sqlite_wheels=("$wheels"/rowx_sqlite-*.whl)
cd "$work/empty"
uv run --no-project --refresh --python 3.14 --with "${rowx_wheels[0]}[sqlite]" --with "${core_wheels[0]}" --with "${sqlite_wheels[0]}" python -I "$root/scripts/smoke-sqlite-wheel.py"
