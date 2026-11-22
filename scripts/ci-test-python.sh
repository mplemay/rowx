#!/usr/bin/env bash
set -euo pipefail

core_tests=packages/rowx-core/src/rowx_core/__tests__
postgres_tests=packages/rowx-postgres/src/rowx_postgres/__tests__
sqlite_tests=packages/rowx-sqlite/src/rowx_sqlite/__tests__
embedded_tests=packages/rowx-embedded/src/rowx_embedded/__tests__
plugin_tests="$postgres_tests/integration/test_testing.py"

uv sync --frozen --no-install-package rowx-postgres --no-install-package rowx-sqlite --no-install-package rowx-embedded
case "$CI_PACKAGE" in
  postgres | cli | embedded) uv run maturin develop --locked -m packages/rowx-postgres/Cargo.toml ;;
esac
case "$CI_PACKAGE" in
  sqlite | cli) uv run maturin develop --locked -m packages/rowx-sqlite/Cargo.toml ;;
esac

case "$CI_PACKAGE" in
  core) uv run pytest "$core_tests" ;;
  postgres)
    if [[ "${ROWX_REQUIRE_DB:-}" == 1 ]]; then
      uv run pytest "$postgres_tests" --ignore "$plugin_tests" -m 'not embedded' -n 2
      # The plugin tests launch their own subprocesses and workers.
      uv run pytest "$plugin_tests" -m 'not embedded'
    else
      uv run pytest "$postgres_tests" --ignore "$plugin_tests" -m 'not db and not embedded'
    fi
    ;;
  sqlite) uv run pytest "$sqlite_tests" -o faulthandler_timeout=60 ;;
  cli) uv run pytest src/rowx/__tests__ -m 'not embedded' ;;
  embedded)
    uv pip install --no-deps dist-embedded/*.whl
    # Wheels exclude tests. Make the checkout's support modules importable under
    # the installed package while collecting tests from their original paths.
    uv run python scripts/ci-install-embedded-test-support.py
    uv run pytest "$embedded_tests" -n 2
    uv run pytest -m embedded \
      src/rowx/__tests__/unit/test_embedded.py \
      src/rowx/__tests__/unit/check/test_compat.py \
      src/rowx/__tests__/unit/check/test_embedded.py
    ROWX_TEST_DSN=embedded:// ROWX_REQUIRE_DB=1 uv run pytest "$postgres_tests" -m '(db and not server) or embedded' -n 2

    if [[ "$(uv run python -c 'import sys; print(sys.version_info.minor)')" == 14 ]]; then
      uv build --package rowx-core --wheel --out-dir dist-smoke
      uv run maturin build --locked -m packages/rowx-postgres/Cargo.toml --out dist-smoke
      cp dist-embedded/*.whl dist-smoke/
      scripts/smoke-embedded-wheel.sh dist-smoke

      uv run python examples/embedded/notes.py
      cd examples/embedded
      export DATABASE_URL=embedded://
      uv run --project ../.. rowx check -j 4
      uv run --project ../.. pytest -p no:cacheprovider -o rowx_require_database=true
    fi
    ;;
  *)
    echo "Unknown Python package: $CI_PACKAGE" >&2
    exit 2
    ;;
esac
