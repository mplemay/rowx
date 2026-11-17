#!/usr/bin/env bash
# Fail when docs/changelog.md differs from an offline git-cliff render.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

# Locked changelog tools only. --inexact keeps a developer environment, and
# --no-install-workspace skips the native packages. --no-sync keeps uv run from
# installing the default groups; release.yml does not set UV_NO_SYNC.
uv sync --frozen --inexact --only-group changelog --no-install-workspace

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT
generated="$workdir/changelog.md"

uv run --frozen --no-sync python scripts/release.py render-changelog -o "$generated"
diff -u docs/changelog.md "$generated"
