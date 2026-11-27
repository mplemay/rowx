#!/usr/bin/env bash
# Tag a release commit. GH_TOKEN must be an app token: its tag starts release.yml, a GITHUB_TOKEN tag does not.
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
  *)
    echo "usage: $0 fetch-main | create-tag" >&2
    exit 2
    ;;
esac
