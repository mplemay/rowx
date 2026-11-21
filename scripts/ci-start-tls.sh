#!/usr/bin/env bash
set -euo pipefail

dsn="$(scripts/pg.sh up --ssl)"
echo "ROWX_TEST_DSN_SSL=$dsn" >>"$GITHUB_ENV"
