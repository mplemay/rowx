#!/usr/bin/env bash
set -euo pipefail

# The Cargo config's default points to a Unix venv layout. setup-uv selects a
# valid interpreter everywhere, including Windows where PyO3 needs python.lib.
export PYO3_PYTHON
PYO3_PYTHON="$(uv python find 3.14)"

case "$CI_PACKAGE" in
  core) cargo test --locked -p rowx-core -p rowx-py-core ;;
  postgres) cargo test --locked -p rowx-postgres -p rowx-postgres-py ;;
  sqlite) cargo test --locked -p rowx-sqlite -p rowx-sqlite-py ;;
  embedded)
    cargo test --locked -p rowx-embedded-host
    # Use the same hooks as just check-embedded with the locked prek version.
    uvx prek@0.5.10 run cargo-fmt cargo-clippy-embedded --hook-stage manual --all-files
    ;;
  *)
    echo "Unknown Rust package: $CI_PACKAGE" >&2
    exit 2
    ;;
esac
