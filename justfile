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

# start postgres:18 and print the DSN (stub until T03)
db-up:
    @echo "TODO: lands in T03"

# stop the test postgres (stub until T03)
db-down:
    @echo "TODO: lands in T03"

# release build plus bench/fetch_100k.py (stub until T24)
bench:
    @echo "TODO: lands in T24"

# format rust and python through prek
fmt:
    uv run prek run cargo-fmt --all-files
    uv run prek run ruff-format --all-files
