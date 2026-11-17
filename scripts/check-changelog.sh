#!/usr/bin/env bash
# Fail when docs/changelog.md differs from an offline git-cliff render.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

if ! command -v git-cliff >/dev/null 2>&1; then
  echo "git-cliff is not on PATH" >&2
  exit 1
fi

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT
generated="$workdir/changelog.md"

git-cliff --offline -o "$generated"
uvx --from rumdl==0.3.2 rumdl fmt "$generated"
diff -u docs/changelog.md "$generated"
