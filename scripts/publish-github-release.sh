#!/usr/bin/env bash
# Create or update the GitHub release notes for RELEASE_TAG.
set -euo pipefail

if [[ -z ${RELEASE_TAG:-} ]]; then
  echo "RELEASE_TAG is not set" >&2
  exit 2
fi

if [[ -z ${GH_TOKEN:-} ]]; then
  echo "GH_TOKEN is not set" >&2
  exit 2
fi

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT
notes="$workdir/notes.md"

# --strip takes one value; all removes the changelog header and footer
git-cliff --latest --strip all -o "$notes"

if gh release view "$RELEASE_TAG" >/dev/null 2>&1; then
  gh release edit "$RELEASE_TAG" --notes-file "$notes"
else
  gh release create "$RELEASE_TAG" --title "$RELEASE_TAG" --notes-file "$notes"
fi
