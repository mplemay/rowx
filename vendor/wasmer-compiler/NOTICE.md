# Vendored `wasmer-compiler` 7.2.1

This directory is `wasmer-compiler` 7.2.1 from crates.io (MIT, (c) Wasmer Engineering Team), with one change. It is
wired in through `[patch.crates-io]` in the root `Cargo.toml`, and it is exempt from rowx's lint rules.

## What changed

Only `src/engine/unwind/systemv/compact_unwind/mod.rs`, and only the two hunks of the upstream 7.5.0 fix, ported
literally (the 7.5.0 file differs from this one only in an unrelated `sort_by_key` spelling). The stock code sets
`dso_base` to the load address of the image that holds the personality function (found with `dladdr`) and stores
32-bit deltas from it. The patch sets `dso_base` to the lowest of the personality GOT slot, every function address and
every LSDA address, fails the compile with an error when the span exceeds `u32::MAX`, and checks the personality
delta instead of truncating it. The `dladdr` lookup and its `unsafe` block are gone.

## Why

On macOS arm64 the engine is a `cdylib` loaded into CPython. The compiled code is then mapped more than 4 GiB away from
the image, the 32-bit deltas truncate, and libunwind reads garbage while it unwinds a wasm exception. The host process
dies with SIGBUS on any Postgres `ereport` path (a constraint violation, `RAISE`, `SET client_encoding`). A plain Rust
executable, and Linux, are not affected. `wasmer-compiler` 7.5.0 fixes this upstream (this is that code), but
`oliphaunt-wasix` 0.3.2 pins `wasmer =7.2.1`.

## Removing it

Drop the `[patch.crates-io]` entry and this directory once `oliphaunt-wasix` depends on `wasmer >= 7.5.0`.
