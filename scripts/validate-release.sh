#!/usr/bin/env bash
# On a tag push, require a stable vX.Y.Z that points at main, then check that every version file agrees.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

if [[ -z ${EVENT_NAME:-} ]]; then
  echo "EVENT_NAME is not set" >&2
  exit 2
fi

if [[ ${EVENT_NAME} == "push" ]]; then
  if [[ -z ${TAG_NAME:-} ]]; then
    echo "TAG_NAME is not set" >&2
    exit 2
  fi
  if [[ ! ${TAG_NAME} =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]; then
    echo '::error::Release tag must be a stable vX.Y.Z version.'
    exit 1
  fi
  if ! git merge-base --is-ancestor HEAD origin/main; then
    echo '::error::Release tag must point to a commit on main.'
    exit 1
  fi
fi

python "$root/scripts/validate-release-versions.py"
cargo metadata --locked --format-version 1 --no-deps >/dev/null
cargo metadata --locked --format-version 1 --no-deps --manifest-path packages/rowx-embedded/Cargo.toml >/dev/null
