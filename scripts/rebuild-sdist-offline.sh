#!/usr/bin/env bash
# Rebuild one native sdist offline in two phases, kept separate so the build phase can run with CARGO_NET_OFFLINE.
# Usage: rebuild-sdist-offline.sh fetch|build postgres|sqlite
# (RUNNER_TEMP must hold the release-artifacts directory; the fetch phase also needs GITHUB_WORKSPACE)
set -euo pipefail
shopt -s nullglob

usage() {
  echo "usage: $0 fetch|build <distribution-name>" >&2
  exit 2
}

if (($# != 2)); then
  usage
fi
phase="$1"
name="$2"
if [[ ! "$name" =~ ^[a-z]+$ ]]; then
  usage
fi
if [[ -z "${RUNNER_TEMP:-}" ]]; then
  echo "RUNNER_TEMP is not set" >&2
  exit 2
fi

case "$phase" in
  fetch)
    if [[ -z "${GITHUB_WORKSPACE:-}" ]]; then
      echo "GITHUB_WORKSPACE is not set" >&2
      exit 2
    fi
    stage="$RUNNER_TEMP/unpacked-$name"
    mkdir -p "$stage"
    tar -xzf "$RUNNER_TEMP/release-artifacts/rowx_${name}"-*.tar.gz -C "$stage"
    projects=("$stage/rowx_${name}"-*/)
    if ((${#projects[@]} != 1)); then
      echo "expected one rowx-$name sdist" >&2
      exit 1
    fi
    # rustup reads rust-toolchain.toml from the build directory; the sdist is outside the checkout
    cp "$GITHUB_WORKSPACE/rust-toolchain.toml" "${projects[0]}/rust-toolchain.toml"
    # no --locked: the sdist lock omits the unused wasmer-compiler patch, which cargo would otherwise record as a lock change
    cargo fetch --manifest-path "${projects[0]}/Cargo.toml"
    ;;
  build)
    projects=("$RUNNER_TEMP/unpacked-$name/rowx_${name}"-*/)
    if ((${#projects[@]} != 1)); then
      echo "expected one unpacked rowx-$name sdist" >&2
      exit 1
    fi
    cd "${projects[0]}"
    PYO3_PYTHON="$(uv python find 3.14)"
    export PYO3_PYTHON
    maturin build --offline --out "$RUNNER_TEMP/rebuilt-$name"
    ;;
  *)
    usage
    ;;
esac
