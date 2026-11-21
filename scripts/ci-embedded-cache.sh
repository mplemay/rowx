#!/usr/bin/env bash
set -euo pipefail

mkdir -p target/release/deps
find target/release/deps -name 'libwasmer_vm-*.rlib' -exec stat -c '%Y %n' {} + | sort >"$RUNNER_TEMP/wasmer-$1.txt"

case "$1" in
  record) echo "Wasmer artifacts restored from the cache: $(wc -l <"$RUNNER_TEMP/wasmer-record.txt")" ;;
  verify)
    if ! diff "$RUNNER_TEMP/wasmer-record.txt" "$RUNNER_TEMP/wasmer-verify.txt"; then
      echo '::error::Wasmer was rebuilt although the Cargo cache was restored.'
      exit 1
    fi
    ;;
  *)
    echo "Unknown cache check: $1" >&2
    exit 2
    ;;
esac
