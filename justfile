default:
    @just --list

# all checks via prek (ruff, ty, rumdl, cargo fmt, clippy); more gates land with T02 and T04
check:
    uv run prek run --all-files

# cargo test over the workspace; DB tests run when ROWX_TEST_DSN is set (workspace lands in T01)
test-rust:
    cargo test --workspace

# python tests
test-py:
    uv run pytest

# start postgres:18 and print the DSN
db-up:
    @scripts/pg.sh up
    @echo "export ROWX_TEST_DSN=$(scripts/pg.sh dsn)"

# stop the test postgres
db-down:
    @scripts/pg.sh down

# release build plus bench/fetch_100k.py; needs ROWX_TEST_DSN
bench:
    uv sync --group bench --no-install-project
    uv run --no-sync maturin develop --release
    uv run --no-sync python bench/fetch_100k.py

# format rust and python through prek
fmt:
    uv run prek run cargo-fmt --all-files
    uv run prek run ruff-format --all-files
