#!/usr/bin/env bash
set -euo pipefail

uv sync --frozen --no-install-package rowx-postgres --no-install-package rowx-sqlite --no-install-package rowx-embedded
uv run maturin develop --locked -m packages/rowx-postgres/Cargo.toml

EVENT_NAME=pull_request GITHUB_OUTPUT="$RUNNER_TEMP/rowx-release-version.txt" uv run python scripts/validate-release-versions.py
cargo metadata --locked --format-version 1 --no-deps >/dev/null

cargo_tree="$(cargo tree --locked --prefix none --edges all)"
if grep -E '^(wasmer|liboliphaunt|oliphaunt)' <<<"$cargo_tree"; then
  echo '::error::The default workspace members depend on Wasmer or engine crates.'
  exit 1
fi

uv build --package rowx --wheel --out-dir dist
uv build --package rowx-core --wheel --out-dir dist
uv build --package rowx-postgres --wheel --out-dir dist
uv run python scripts/validate-distributions.py --contents-only
bash scripts/smoke-package-boundaries.sh dist

uv venv --python 3.14 "$RUNNER_TEMP/rowx-wheel-types"
rowx_wheels=(dist/rowx-*.whl)
uv pip install --refresh --python "$RUNNER_TEMP/rowx-wheel-types/bin/python" "${rowx_wheels[0]}[postgres]" dist/rowx_core-*.whl dist/rowx_postgres-*.whl
ROWX_TYPING_PYTHON="$RUNNER_TEMP/rowx-wheel-types/bin/python" uv run pytest packages/rowx-postgres/src/rowx_postgres/__tests__/unit/test__executor.py
