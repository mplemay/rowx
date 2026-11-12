import asyncio
import os
import secrets
import shutil
import signal
import subprocess  # ruff: ignore[suspicious-subprocess-import]
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Final

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from rowx_core.lifecycle import ORPHAN_AGE_SECONDS, make_tag
from rowx_postgres import connect
from rowx_postgres.__tests__.integration.check.harness import require_companion
from rowx_postgres._core import create_database, drop_database, list_databases
from rowx_postgres._scratch import EmbeddedScratch
from rowx_postgres._services import PostgresAdmin, PostgresMigrations

from rowx import query_unchecked
from rowx.check.scratch import PREFIX, Scratch

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = [pytest.mark.db]

POLL_LIMIT: Final[float] = 60.0
SLOW_MIGRATION: Final[str] = "SELECT pg_sleep(120);"
CLEAN_QUERY: Final[str] = 'from rowx import query\n\nquery[tuple[int]](t"SELECT 1")\n'


@pytest.fixture
def server_dsn() -> str:
    # an empty DSN and a missing DSN fail differently
    if (dsn := os.environ.get("ROWX_TEST_DSN")) is not None and dsn != "":  # ruff: ignore[compare-to-empty-string]
        return dsn
    if os.environ.get("ROWX_REQUIRE_DB") == "1":
        pytest.fail("ROWX_REQUIRE_DB=1 but ROWX_TEST_DSN is not set")
    pytest.skip("ROWX_TEST_DSN is not set")


def scratch_names(dsn: str) -> set[str]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        rows = conn.execute("SELECT datname FROM pg_database WHERE datname LIKE 'rowx\\_check\\_%'").fetchall()
    return {row[0] for row in rows}


def planted_name() -> str:
    return f"{PREFIX}{secrets.token_hex(8)}"


# the native future binds the running loop when it is created, so the call has to happen inside one
def drop_now(server_dsn: str, name: str) -> None:
    async def run() -> None:
        await drop_database(server_dsn, name, force=True)

    asyncio.run(run())


@pytest.fixture
def planted(server_dsn: str) -> Iterator[list[str]]:
    names: list[str] = []
    try:
        yield names
    finally:
        for name in names:
            drop_now(server_dsn, name)


def test_the_scratch_tag_names_the_process_and_the_time() -> None:
    tag = make_tag()
    assert tag.startswith(f"rowx:{os.getpid()}@")
    assert abs(int(tag.rpartition(":")[2]) - time.time()) < 60


@pytest.mark.server(needs="create-database")
def test_an_old_orphan_is_swept_and_a_fresh_one_is_kept(server_dsn: str, planted: list[str]) -> None:
    now = int(time.time())
    old, fresh, busy, untagged = (planted_name() for _ in range(4))
    comments = {
        old: f"rowx:1@gone:{now - ORPHAN_AGE_SECONDS - 60}",
        fresh: f"rowx:1@gone:{now}",
        busy: f"rowx:1@gone:{now - ORPHAN_AGE_SECONDS - 60}",
        untagged: None,
    }
    planted.extend(comments)

    async def run() -> None:
        for name, comment in comments.items():
            await create_database(server_dsn, name, comment=comment)
        # a session keeps an old orphan from being swept
        with psycopg.connect(make_conninfo(server_dsn, dbname=busy)):
            await Scratch(server_dsn, PostgresAdmin(server_dsn)).sweep_orphans()

    asyncio.run(run())
    left = scratch_names(server_dsn)
    assert old not in left
    assert {fresh, busy, untagged} <= left


@pytest.mark.server(needs="create-database")
def test_a_scratch_database_is_tagged_while_it_exists(server_dsn: str, tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "1_noop.sql").write_text("SELECT 1;", encoding="utf-8")
    scratch = Scratch(server_dsn, PostgresAdmin(server_dsn))
    tags: list[str | None] = []

    async def run() -> None:
        async with scratch.session(PostgresMigrations(server_dsn, migrations)):
            tags.extend(info.comment for info in await list_databases(server_dsn, scratch.name))

    asyncio.run(run())
    assert len(tags) == 1
    assert tags[0] is not None
    assert tags[0].startswith(f"rowx:{os.getpid()}@")
    assert scratch.name not in scratch_names(server_dsn)


def new_scratch(dsn: str, before: set[str]) -> str | None:
    found = scratch_names(dsn) - before
    return next(iter(found), None)


# the test polls the server while the child it started holds a session, so the two compete for the one an instance has
@pytest.mark.server(needs="sessions")
@pytest.mark.server(needs="create-database")
def test_sigint_drops_the_scratch_database_and_exits_two(server_dsn: str, tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.rowx.migrate]\n", encoding="utf-8")
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    # holds the run inside the scratch build, with its database already created
    (migrations / "1_slow.sql").write_text(SLOW_MIGRATION, encoding="utf-8")
    (tmp_path / "q.py").write_text(CLEAN_QUERY, encoding="utf-8")
    before = scratch_names(server_dsn)
    rowx = Path(sys.executable).parent / "rowx"
    proc = subprocess.Popen(  # ruff: ignore[subprocess-without-shell-equals-true]
        [str(rowx), "check", "q.py", "--database-url", server_dsn, "--scratch"],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # each poll is a query round trip, so no sleep is needed
        deadline = time.monotonic() + POLL_LIMIT
        name = None
        while name is None and proc.poll() is None and time.monotonic() < deadline:
            name = new_scratch(server_dsn, before)
        assert name is not None, "the scratch database never appeared in pg_database"
        proc.send_signal(signal.SIGINT)
        out, err = proc.communicate(timeout=POLL_LIMIT)
        # read before the cleanup below, which would otherwise hide a database the run failed to drop
        left = scratch_names(server_dsn)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
        for leaked in scratch_names(server_dsn) - before:
            drop_now(server_dsn, leaked)
    assert proc.returncode == 2
    assert not out
    assert "interrupted" in err
    assert name not in left


SLOW_EMBEDDED_MIGRATION: Final[str] = "CREATE TABLE t (id int4); SELECT pg_sleep(30);"
BOOT_SECONDS: Final[float] = 3.0


@pytest.fixture(scope="module")
def companion() -> None:
    require_companion()


# a short path of its own (a socket path is limited to about 100 bytes), so a leftover can be counted exactly
@pytest.fixture
def private_tmp(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    root = Path(tempfile.mkdtemp(prefix="rxs-"))
    monkeypatch.setenv("TMPDIR", str(root))
    yield root
    shutil.rmtree(root, ignore_errors=True)


def socket_directories(root: Path) -> int:
    return len(list(root.glob("rxe-*")))


def migrations_in(tmp_path: Path, migration: str) -> Path:
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "1_t.sql").write_text(migration, encoding="utf-8")
    return directory


@pytest.mark.embedded
@pytest.mark.usefixtures("companion")
@pytest.mark.parametrize("workers", [1, 3])
def test_an_embedded_scratch_builds_one_independent_migrated_instance_per_worker_and_removes_them(
    workers: int, tmp_path: Path, private_tmp: Path
) -> None:
    migrations = migrations_in(tmp_path, "CREATE TABLE t (id int4);")
    scratch = EmbeddedScratch()
    during: list[int] = []

    async def run() -> list[int]:
        counts: list[int] = []
        async with scratch.session(PostgresMigrations("embedded://", migrations), workers) as built:
            # exactly one instance, and so one socket directory, per worker
            assert len(built.urls) == workers
            during.append(socket_directories(private_tmp))
            for position, url in enumerate(built.urls):
                async with await connect(url) as conn:
                    await query_unchecked[None](t"INSERT INTO t VALUES ({position})").execute(conn)
            for url in built.urls:
                async with await connect(url) as conn:
                    counts.append(await query_unchecked[int](t"SELECT count(*)::int4 FROM t").one(conn))
        return counts

    # each instance holds only its own row
    assert asyncio.run(run()) == [1] * workers
    assert during == [workers]
    assert not scratch.leaked
    assert list(private_tmp.iterdir()) == []


@pytest.mark.embedded
@pytest.mark.usefixtures("companion")
def test_cancelling_a_scratch_mid_migration_returns_at_once_and_removes_the_instance(
    tmp_path: Path, private_tmp: Path
) -> None:
    migrations = migrations_in(tmp_path, SLOW_EMBEDDED_MIGRATION)
    scratch = EmbeddedScratch()

    async def run() -> float:
        async def build() -> None:
            async with scratch.session(PostgresMigrations("embedded://", migrations), 2):
                pass

        task = asyncio.ensure_future(build())
        # boot, connect and start the migration, which is then 30 seconds long
        await asyncio.sleep(BOOT_SECONDS)
        cancelled = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return time.monotonic() - cancelled

    assert asyncio.run(run()) < 2.0
    assert not scratch.leaked
    assert list(private_tmp.iterdir()) == []
