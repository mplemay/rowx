#!/usr/bin/env bash
# Tag a release commit and dispatch Release. A GITHUB_TOKEN tag push does not start release.yml.
set -euo pipefail

require_tag() {
  if [[ -z ${TAG_NAME:-} ]]; then
    echo "TAG_NAME is not set" >&2
    exit 2
  fi
  if [[ ! ${TAG_NAME} =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]; then
    echo "refusing unexpected tag name" >&2
    exit 1
  fi
}

cmd="${1:-}"
case "$cmd" in
  fetch-main)
    git fetch --no-tags origin "+refs/heads/main:refs/remotes/origin/main"
    if ! git merge-base --is-ancestor HEAD origin/main; then
      git fetch --no-tags --unshallow origin "+refs/heads/main:refs/remotes/origin/main"
    fi
    ;;
  create-tag)
    require_tag
    if [[ -z ${HEAD_SHA:-} || -z ${REPOSITORY:-} ]]; then
      echo "HEAD_SHA and REPOSITORY are required" >&2
      exit 2
    fi
    gh api --method POST "repos/${REPOSITORY}/git/refs" -f "ref=refs/tags/${TAG_NAME}" -f "sha=${HEAD_SHA}"
    ;;
  dispatch)
    require_tag
    for attempt in 1 2 3 4 5; do
      if gh workflow run release.yml --ref "${TAG_NAME}"; then
        exit 0
      fi
      sleep "$((attempt * 2))"
    done
    echo "could not dispatch Release on ${TAG_NAME}" >&2
    exit 1
    ;;
  *)
    echo "usage: $0 fetch-main | create-tag | dispatch" >&2
    exit 2
    ;;
esac
