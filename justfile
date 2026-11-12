default:
    @just --list

# all checks via prek (ruff, ty, rumdl, shellcheck, shfmt, cargo fmt, clippy)
check:
    uv run prek run --all-files

# clippy for packages/rowx-embedded (a separate workspace: builds the Wasmer tree, so `check` leaves it out)
check-embedded:
    uv run prek run cargo-clippy-embedded --hook-stage manual --all-files

# cargo test over the workspace; DB tests run when ROWX_TEST_DSN is set
test-rust:
    cargo test --workspace

# python tests
test-py:
    uv run pytest

# install the local rowx-embedded companion (a maturin build of the Wasmer engine; `uv sync --all-groups` never does)
embedded-dev:
    uv sync --extra embedded

# the tests marked `embedded`; they fail instead of skipping when the companion is missing
test-embedded:
    ROWX_REQUIRE_EMBEDDED=1 uv run --extra embedded pytest -m embedded

# long nullability fuzz with a random seed, printing the precision report; needs ROWX_TEST_DSN
fuzz-nullability count="5000":
    ROWX_FUZZ_REPORT=1 ROWX_FUZZ_COUNT={{count}} ROWX_FUZZ_SEED=random uv run pytest packages/rowx-postgres/src/rowx_postgres/__tests__/integration/describe/test_nullability_fuzz.py -s

# start postgres:18 and print the DSN
db-up:
    @scripts/pg.sh up
    @echo "export ROWX_TEST_DSN=$(scripts/pg.sh dsn)"

# stop the test postgres
db-down:
    @scripts/pg.sh down

# release build plus bench/fetch_100k.py and bench/pool_one.py; needs ROWX_TEST_DSN
bench:
    uv sync --all-packages --no-install-package rowx-embedded --group bench
    uv run --no-sync maturin develop -m packages/rowx-postgres/Cargo.toml --release
    uv run --no-sync python bench/fetch_100k.py
    uv run --no-sync python bench/pool_one.py

# release build plus bench/fetch_drivers.py; needs ROWX_TEST_DSN
bench-drivers:
    uv sync --all-packages --no-install-package rowx-embedded --group bench-drivers
    uv run --no-sync maturin develop -m packages/rowx-postgres/Cargo.toml --release
    uv run --no-sync python bench/fetch_drivers.py

# format rust, python, and shell through prek
fmt:
    uv run prek run cargo-fmt --all-files
    uv run prek run ruff-format --all-files
    uv run prek run shfmt --all-files

# serve the documentation site
docs:
    uv run --group docs zensical serve

# build the documentation site in strict mode
docs-build:
    uv run --group docs zensical build --clean --strict
