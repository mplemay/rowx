# Embedded Postgres benchmarks (#203): cold start, boot from a snapshot, `rowx check` on examples/embedded (embedded at
# `-j 4` against a server) and fetch_100k over embedded. The numbers are recorded, never gated.
#
#   uv sync --all-packages --extra embedded && uv run maturin develop -m packages/rowx-postgres/Cargo.toml --release
#   uv run --no-sync python bench/embedded.py [--only cold boot check fetch] [--server-dsn DSN]
#
# `--server-dsn` (default: ROWX_TEST_DSN when it is a server) adds the server rows; the role needs CREATEDB.
import argparse
import asyncio
import json
import os
import shutil
import statistics
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from secrets import token_hex
from subprocess import run  # ruff: ignore[suspicious-subprocess-import]
from time import perf_counter
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit, urlunsplit

from rowx_postgres import Migrator, connect

import rowx

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

ROOT: Final[Path] = Path(__file__).resolve().parent.parent
EXAMPLE: Final[Path] = ROOT / "examples" / "embedded"
PROCESSES: Final[int] = 5
BOOTS: Final[int] = 20
CHECK_RUNS: Final[int] = 7
ROWS: Final[int] = 100_000
FETCH_RUNS: Final[int] = 7
JOBS: Final[int] = 4
SECTIONS: Final[tuple[str, ...]] = ("cold", "boot", "check", "fetch")


def ms(seconds: float) -> float:
    return seconds * 1000


def say(line: str = "") -> None:
    sys.stdout.write(f"{line}\n")


def line_of(label: str, samples: Sequence[float]) -> str:
    return (
        f"{label:<44} median {statistics.median(samples):8.1f} ms   min {min(samples):8.1f} ms"
        f"   max {max(samples):8.1f} ms   n={len(samples)}"
    )


# --- cold start -------------------------------------------------------------------------------------------------------


# runs in a fresh interpreter: nothing of the engine is loaded or cached yet
async def child_cold() -> dict[str, float]:
    start = perf_counter()
    from rowx_embedded import Instance  # ruff: ignore[import-outside-top-level]

    imported = perf_counter()
    instance = Instance.start()
    started = perf_counter()
    async with await connect(instance.dsn) as conn:
        await rowx.query[int](t"SELECT 1").one(conn)
    queried = perf_counter()
    instance.close()
    closed = perf_counter()
    return {
        "import": ms(imported - start),
        "start": ms(started - imported),
        "first query": ms(queried - started),
        "ready": ms(queried - start),
        "close": ms(closed - queried),
    }


def run_child(mode: str, *arguments: str) -> dict[str, float]:
    result = run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [sys.executable, str(Path(__file__).resolve()), "--child", mode, *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout.splitlines()[-1])


def section_cold() -> None:
    say("Cold start: a fresh interpreter, `Instance.start()` with no snapshot (initdb in memory), then one query")
    runs = [run_child("cold") for _ in range(PROCESSES)]
    for key in ("import", "start", "first query", "ready", "close"):
        say(line_of(f"  {key}", [row[key] for row in runs]))
    say("  (import: `from rowx_embedded import Instance`; ready: process work from the import to the first row)")


# --- boot from a snapshot ---------------------------------------------------------------------------------------------


async def migrated_snapshot() -> bytes:
    from rowx_embedded import Instance  # ruff: ignore[import-outside-top-level]

    instance = Instance.start()
    try:
        async with await connect(instance.dsn) as conn:
            await Migrator(EXAMPLE / "migrations").run(conn)
        return instance.snapshot()
    finally:
        instance.close()


async def child_boot(snapshot: bytes) -> dict[str, float]:
    from rowx_embedded import Instance  # ruff: ignore[import-outside-top-level]

    boots: list[float] = []
    usable: list[float] = []
    teardowns: list[float] = []
    kinds: set[str] = set()
    for _ in range(BOOTS):
        start = perf_counter()
        instance = Instance.start(snapshot=snapshot)
        booted = perf_counter()
        async with await connect(instance.dsn) as conn:
            await rowx.query[int](t"SELECT count(*) FROM pg_tables WHERE tablename = 'notes'").one(conn)
        queried = perf_counter()
        instance.close()
        teardowns.append(ms(perf_counter() - queried))
        boots.append(ms(booted - start))
        usable.append(ms(queried - start))
        kinds.add(instance.boot)
    if kinds != {"seed"}:
        sys.stderr.write(f"warning: boot kinds were {sorted(kinds)}\n")
    return {
        "first_boot": boots[0],
        "first_usable": usable[0],
        "later_boot": statistics.median(boots[1:]),
        "later_usable": statistics.median(usable[1:]),
        "later_close": statistics.median(teardowns[1:]),
        "first_close": teardowns[0],
    }


def section_boot() -> None:
    snapshot = asyncio.run(migrated_snapshot())
    say(f"Boot from a snapshot of the migrated example schema ({len(snapshot) / 1e6:.1f} MB, {len(snapshot)} bytes)")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bench.snapshot"
        path.write_bytes(snapshot)
        runs = [run_child("boot", str(path)) for _ in range(PROCESSES)]
    say(line_of("  first boot in a process (Instance.start)", [row["first_boot"] for row in runs]))
    say(line_of("  first boot, to the first query", [row["first_usable"] for row in runs]))
    say(line_of("  later boots in the process (Instance.start)", [row["later_boot"] for row in runs]))
    say(line_of("  later boots, to the first query", [row["later_usable"] for row in runs]))
    say(line_of("  close after the first boot", [row["first_close"] for row in runs]))
    say(line_of("  close after later boots", [row["later_close"] for row in runs]))
    say(
        f"  ({PROCESSES} fresh processes; each boots {BOOTS} instances in turn; 'later' is the median of boots 2 to {BOOTS})"
    )


# --- rowx check on the example ----------------------------------------------------------------------------------------


def rowx_executable() -> str:
    beside = Path(sys.executable).with_name("rowx")
    return str(beside) if beside.is_file() else shutil.which("rowx") or "rowx"


def timed_rowx(url: str, *arguments: str, expect: int = 0) -> float:
    start = perf_counter()
    result = run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [rowx_executable(), *arguments],
        check=False,
        capture_output=True,
        text=True,
        cwd=EXAMPLE,
        env={**os.environ, "DATABASE_URL": url},
    )
    elapsed = ms(perf_counter() - start)
    if result.returncode != expect:
        msg = f"`rowx {' '.join(arguments)}` exited {result.returncode}: {result.stdout}{result.stderr}"
        raise RuntimeError(msg)
    return elapsed


def with_database(dsn: str, name: str) -> str:
    parts = urlsplit(dsn)
    return urlunsplit(parts._replace(path=f"/{name}"))


def section_check(server_dsn: str | None) -> None:
    say(
        f"`rowx check` on examples/embedded (7 queries, 2 migrations), wall time of the whole process, {CHECK_RUNS} runs"
    )
    say(
        line_of(
            "  rowx check --version (interpreter and imports only)",
            [timed_rowx("", "check", "--version") for _ in range(CHECK_RUNS)],
        )
    )
    timed_rowx("embedded://", "check", "-j", str(JOBS))
    for jobs in (JOBS, 1):
        samples = [timed_rowx("embedded://", "check", "-j", str(jobs)) for _ in range(CHECK_RUNS)]
        say(line_of(f"  embedded://  -j {jobs}  (implied --scratch)", samples))
    if server_dsn is None:
        say("  (no server DSN: the Docker rows are skipped)")
        return
    name = f"rowx_bench_{token_hex(4)}"
    admin = with_database(server_dsn, name)
    timed_rowx(server_dsn, "check", "--scratch", "-j", str(JOBS))
    samples = [timed_rowx(server_dsn, "check", "--scratch", "-j", str(JOBS)) for _ in range(CHECK_RUNS)]
    say(line_of(f"  server --scratch -j {JOBS}  (create, migrate, drop)", samples))
    timed_rowx(admin, "database", "create")
    try:
        timed_rowx(admin, "migrate", "run")
        timed_rowx(admin, "check", "-j", str(JOBS))
        samples = [timed_rowx(admin, "check", "-j", str(JOBS)) for _ in range(CHECK_RUNS)]
        say(line_of(f"  server, migrated database  -j {JOBS}  (drift gate)", samples))
    finally:
        timed_rowx(admin, "database", "drop", "-y", "--force")


# --- fetch_100k over embedded -----------------------------------------------------------------------------------------


async def median_ms(run_once: Callable[[], Awaitable[int]]) -> float:
    # one untimed warmup so the statement is prepared and cached
    await run_once()
    samples: list[float] = []
    for _ in range(FETCH_RUNS):
        start = perf_counter()
        count = await run_once()
        samples.append(ms(perf_counter() - start))
        if count != ROWS:
            msg = f"expected {ROWS} rows, got {count}"
            raise RuntimeError(msg)
    return statistics.median(samples)


async def fetch_numbers(dsn: str) -> dict[str, float]:
    select = rowx.query[tuple[int, str, datetime]](t"SELECT id, body, created_at FROM rowx_bench_100k")
    scalar = rowx.query[int](t"SELECT id FROM rowx_bench_100k")
    async with await connect(dsn) as conn:
        await rowx.query(t"DROP TABLE IF EXISTS rowx_bench_100k").execute(conn)
        await rowx.query(
            t"CREATE TABLE rowx_bench_100k (id int4 NOT NULL, body text NOT NULL, created_at timestamptz NOT NULL)"
        ).execute(conn)
        await rowx.query(
            t"""
            INSERT INTO rowx_bench_100k
            SELECT i, 'row ' || i, now() + make_interval(secs => i) FROM generate_series(1, 100000) i
            """
        ).execute(conn)
        try:

            async def all_rows() -> int:
                return len(await select.all(conn))

            async def streamed() -> int:
                return len([row async for row in select.fetch(conn)])

            async def scalars() -> int:
                return len(await scalar.all(conn))

            return {
                "all": await median_ms(all_rows),
                "fetch": await median_ms(streamed),
                "scalar all": await median_ms(scalars),
            }
        finally:
            await rowx.query(t"DROP TABLE IF EXISTS rowx_bench_100k").execute(conn)


async def embedded_fetch() -> dict[str, float]:
    from rowx_postgres.embedded import Database  # ruff: ignore[import-outside-top-level]

    async with Database() as database:
        return await fetch_numbers(database.dsn)


def section_fetch(server_dsn: str | None) -> None:
    say(f"fetch_100k: {ROWS} rows of (int4, text, timestamptz), rowx only, median of {FETCH_RUNS} runs after a warmup")
    numbers = {"embedded": asyncio.run(embedded_fetch())}
    if server_dsn is not None:
        numbers["server"] = asyncio.run(fetch_numbers(server_dsn))
    say(f"  {'':<12}" + "".join(f"{key:>14}" for key in numbers["embedded"]))
    for target, row in numbers.items():
        say(f"  {target:<12}" + "".join(f"{value:>11.1f} ms" for value in row.values()))
    if "server" in numbers:
        say(
            f"  {'ratio':<12}"
            + "".join(f"{numbers['embedded'][key] / numbers['server'][key]:>13.2f}x" for key in numbers["embedded"])
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="embedded Postgres benchmarks; the numbers are recorded, not gated")
    parser.add_argument("--only", nargs="+", choices=SECTIONS, default=list(SECTIONS))
    parser.add_argument("--server-dsn", default=os.environ.get("ROWX_TEST_DSN"), help="a real server to compare with")
    parser.add_argument("--child", choices=("cold", "boot"), help=argparse.SUPPRESS)
    parser.add_argument("snapshot", nargs="?", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child == "cold":
        say(json.dumps(asyncio.run(child_cold())))
        return
    if args.child == "boot":
        say(json.dumps(asyncio.run(child_boot(Path(args.snapshot).read_bytes()))))
        return
    server_dsn = None if args.server_dsn is None or args.server_dsn.startswith("embedded:") else args.server_dsn
    say(f"load average at the start: {os.getloadavg()}")
    for section in args.only:
        say()
        match section:
            case "cold":
                section_cold()
            case "boot":
                section_boot()
            case "check":
                section_check(server_dsn)
            case "fetch":
                section_fetch(server_dsn)
    say()
    say(f"load average at the end: {os.getloadavg()}")


if __name__ == "__main__":
    main()
