#!/usr/bin/env bash
set -euo pipefail

scripts/start-smoke-postgres.sh
{
  echo 'ROWX_REQUIRE_DB=1'
  echo 'ROWX_TEST_DSN=postgresql://rowx:rowx@localhost:5432/rowx'
} >>"$GITHUB_ENV"
