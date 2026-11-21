#!/usr/bin/env bash
set -euo pipefail

rustup show active-toolchain || rustup toolchain install
