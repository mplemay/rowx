#!/usr/bin/env bash
# --ignored exits 0 when the name matches nothing, so require exactly one passing test.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

log="$root/protocol-limits.log"
cargo test -p rowx-postgres --test exec -- --ignored --exact parameter_of_2_pow_31_bytes_errors_and_keeps_connection 2>&1 \
  | tee "$log"
grep -q "test result: ok. 1 passed" "$log"
