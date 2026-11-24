#!/usr/bin/env bash
# Install the four distributions from dist/ outside the repo and confirm they import.
# The import runs in RUNNER_TEMP so a checkout cannot shadow the wheel with src/rowx.
set -euo pipefail

if [[ -z ${GITHUB_WORKSPACE:-} ]]; then
  echo "GITHUB_WORKSPACE is not set" >&2
  exit 2
fi
if [[ -z ${RUNNER_TEMP:-} ]]; then
  echo "RUNNER_TEMP is not set" >&2
  exit 2
fi

wheels=("$GITHUB_WORKSPACE"/dist/*.whl)
rowx_wheels=("$GITHUB_WORKSPACE"/dist/rowx-*.whl)
core_wheels=("$GITHUB_WORKSPACE"/dist/rowx_core-*.whl)
postgres_wheels=("$GITHUB_WORKSPACE"/dist/rowx_postgres-*.whl)
sqlite_wheels=("$GITHUB_WORKSPACE"/dist/rowx_sqlite-*.whl)
if [[ ${#wheels[@]} != 4 || ! -f "${wheels[0]:-}" ]]; then
  echo '::error::Expected the CLI, core, PostgreSQL and SQLite wheels.'
  exit 1
fi

cd "$RUNNER_TEMP"
with_wheels=(--with "${rowx_wheels[0]}[postgres,sqlite]" --with "${core_wheels[0]}" --with "${postgres_wheels[0]}" --with "${sqlite_wheels[0]}")
uv run --no-project --refresh --python 3.14 "${with_wheels[@]}" python -c 'import rowx, rowx_core, rowx_postgres, rowx_postgres._core, rowx_sqlite, rowx_sqlite._core; assert rowx.query is rowx_core.query'
uv run --no-project --refresh --python 3.14 "${with_wheels[@]}" rowx --help
# a file database from an empty directory, so Windows path handling is exercised on every platform
mkdir -p smoke-sqlite-empty
cd smoke-sqlite-empty
uv run --no-project --refresh --python 3.14 "${with_wheels[@]}" python -I "$GITHUB_WORKSPACE/scripts/smoke-sqlite-wheel.py"
