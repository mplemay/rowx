#!/usr/bin/env bash
set -euo pipefail

command -v cargo-deny >/dev/null || cargo install --locked --version 0.20.2 cargo-deny
cargo deny --workspace check licenses
