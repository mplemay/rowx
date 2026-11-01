import asyncio
import os
import secrets
import signal
import subprocess  # ruff: ignore[suspicious-subprocess-import]
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Final

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from rowx.check.scratch import ORPHAN_AGE_SECONDS, PREFIX, Scratch, make_tag
from rowx.postgres import Migrator
from rowx.postgres._admin import create_database, drop_database, list_databases

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = [pytest.mark.integration, pytest.mark.db]

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


@pytest.fixture
def planted(server_dsn: str) -> Iterator[list[str]]:
    names: list[str] = []
    try:
        yield names
    finally:
        for name in names:
            asyncio.run(drop_database(server_dsn, name, force=True))


def test_the_scratch_tag_names_the_process_and_the_time() -> None:
    tag = make_tag()
    assert tag.startswith(f"rowx:{os.getpid()}@")
    assert abs(int(tag.rpartition(":")[2]) - time.time()) < 60


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
            await Scratch(server_dsn).sweep_orphans()

    asyncio.run(run())
    left = scratch_names(server_dsn)
    assert old not in left
    assert {fresh, busy, untagged} <= left


def test_a_scratch_database_is_tagged_while_it_exists(server_dsn: str, tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "1_noop.sql").write_text("SELECT 1;", encoding="utf-8")
    scratch = Scratch(server_dsn)
    tags: list[str | None] = []

    async def run() -> None:
        async with scratch.session(Migrator(migrations)):
            tags.extend(info.comment for info in await list_databases(server_dsn, scratch.name))

    asyncio.run(run())
    assert len(tags) == 1
    assert tags[0] is not None
    assert tags[0].startswith(f"rowx:{os.getpid()}@")
    assert scratch.name not in scratch_names(server_dsn)


def new_scratch(dsn: str, before: set[str]) -> str | None:
    found = scratch_names(dsn) - before
    return next(iter(found), None)


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
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
        for leaked in scratch_names(server_dsn) - before:
            asyncio.run(drop_database(server_dsn, leaked, force=True))
    assert proc.returncode == 2
    assert not out
    assert "interrupted" in err
    assert name not in scratch_names(server_dsn)
