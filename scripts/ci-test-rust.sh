#!/usr/bin/env bash
set -euo pipefail

# Install the matrix interpreter before uv sync creates the virtual environment.
# PyO3 needs the native interpreter path, including on Windows.
uv python install "$CI_PYTHON_VERSION"
export PYO3_PYTHON
PYO3_PYTHON="$(uv python find "$CI_PYTHON_VERSION")"

case "$CI_PACKAGE" in
  core) cargo test --locked -p rowx-core -p rowx-py-core ;;
  postgres) cargo test --locked -p rowx-postgres -p rowx-postgres-py ;;
  sqlite) cargo test --locked -p rowx-sqlite -p rowx-sqlite-py ;;
  embedded)
    cargo test --locked -p rowx-embedded-host
    # Use the same hooks as just check-embedded with the locked prek version.
    if [[ "$CI_PYTHON_VERSION" == 3.14 ]]; then
      uvx prek@0.5.10 run cargo-fmt cargo-clippy-embedded --hook-stage manual --all-files
    fi
    ;;
  *)
    echo "Unknown Rust package: $CI_PACKAGE" >&2
    exit 2
    ;;
esac
