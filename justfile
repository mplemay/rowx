default:
    @just --list

# all checks via prek (ruff, ty, rumdl, shellcheck, shfmt, cargo fmt, clippy)
check:
    uv run prek run --all-files

# cargo test over the workspace; DB tests run when ROWX_TEST_DSN is set (workspace lands in T01)
test-rust:
    cargo test --workspace

# python tests
test-py:
    uv run pytest

# long nullability fuzz with a random seed, printing the precision report; needs ROWX_TEST_DSN
fuzz-nullability count="5000":
    ROWX_FUZZ_REPORT=1 ROWX_FUZZ_COUNT={{count}} ROWX_FUZZ_SEED=$RANDOM uv run pytest src/rowx/__tests__/integration/describe/test_nullability_fuzz.py -s

# start postgres:18 and print the DSN
db-up:
    @scripts/pg.sh up
    @echo "export ROWX_TEST_DSN=$(scripts/pg.sh dsn)"

# stop the test postgres
db-down:
    @scripts/pg.sh down

# release build plus bench/fetch_100k.py and bench/pool_one.py; needs ROWX_TEST_DSN
bench:
    uv sync --group bench --no-install-project
    uv run --no-sync maturin develop --release
    uv run --no-sync python bench/fetch_100k.py
    uv run --no-sync python bench/pool_one.py

# release build plus bench/fetch_drivers.py; needs ROWX_TEST_DSN
bench-drivers:
    uv sync --group bench-drivers --no-install-project
    uv run --no-sync maturin develop --release
    uv run --no-sync python bench/fetch_drivers.py

# format rust, python, and shell through prek
fmt:
    uv run prek run cargo-fmt --all-files
    uv run prek run ruff-format --all-files
    uv run prek run shfmt --all-files
