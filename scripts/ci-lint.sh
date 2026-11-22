#!/usr/bin/env bash
set -euo pipefail

uv sync --frozen --all-packages --all-groups --no-install-package rowx-postgres --no-install-package rowx-sqlite --no-install-package rowx-embedded
uv run maturin develop --locked -m packages/rowx-postgres/Cargo.toml
uv run maturin develop --locked -m packages/rowx-sqlite/Cargo.toml
uv pip install --no-deps dist-embedded/*.whl
uv run python scripts/ci-install-embedded-test-support.py
just check
