#!/usr/bin/env bash
# Install the single wheel in dist/ outside the repo and confirm it imports.
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
if [[ ${#wheels[@]} != 1 || ! -f "${wheels[0]:-}" ]]; then
  echo '::error::Expected exactly one wheel.'
  exit 1
fi

wheel="${wheels[0]}"
cd "$RUNNER_TEMP"
uv run --no-project --python 3.14 --with "$wheel" python -c 'import rowx, rowx.postgres, rowx.postgres._core'
uv run --no-project --python 3.14 --with "$wheel" rowx --help
