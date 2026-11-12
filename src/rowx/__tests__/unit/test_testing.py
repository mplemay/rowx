import asyncio
import signal
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from rowx_core.migrations import Migration, discover_migrations

from rowx.check.config import DEFAULT_MIGRATE_TABLE
from rowx.testing import TEMPLATE_PREFIX, blocking, project_prefix, template_name

if TYPE_CHECKING:
    from collections.abc import Iterable


LONG_WAIT: Final[float] = 30.0
INTERRUPT_DELAY: Final[float] = 0.2


@dataclass(slots=True, frozen=True)
class MigrationFiles:
    migrations: tuple[Migration, ...]


def source(path: Path) -> MigrationFiles:
    return MigrationFiles(discover_migrations(path))


def migrations(path: Path) -> Path:
    path.mkdir()
    (path / "1_a.sql").write_text("SELECT 1;", encoding="utf-8")
    return path


def name_of(path: Path, *, table: str = DEFAULT_MIGRATE_TABLE, create_schemas: Iterable[str] = ()) -> str:
    return template_name(source(path), directory=path, table=table, create_schemas=create_schemas)


def test_template_name_changes_with_the_rowx_version(tmp_path: Path) -> None:
    path = migrations(tmp_path / "migrations")
    first = template_name(source(path), directory=path, rowx="1.0.0")
    assert template_name(source(path), directory=path, rowx="1.0.0") == first
    assert template_name(source(path), directory=path, rowx="1.0.1") != first
    # only the `<h>` half changes: the project prefix is the path alone
    assert template_name(source(path), directory=path, rowx="1.0.1").split("_")[2] == first.split("_")[2]


def test_template_name_changes_with_the_path(tmp_path: Path) -> None:
    first = migrations(tmp_path / "one")
    second = migrations(tmp_path / "two")
    one = template_name(source(first), directory=first, rowx="1.0.0")
    two = template_name(source(second), directory=second, rowx="1.0.0")
    # identical files, different paths: a different project prefix and a different hash
    assert one != two
    assert one.split("_")[2] != two.split("_")[2]
    assert one.split("_")[3] != two.split("_")[3]
    assert one.startswith(f"{TEMPLATE_PREFIX}{project_prefix(first)}_")


def test_template_name_follows_the_migrations_and_their_config(tmp_path: Path) -> None:
    path = migrations(tmp_path / "migrations")
    first = name_of(path)
    assert name_of(path) == first
    (path / "2_b.up.sql").write_text("SELECT 2;", encoding="utf-8")
    (path / "2_b.down.sql").write_text("SELECT 3;", encoding="utf-8")
    with_up = name_of(path)
    assert with_up != first
    # a down file never changes the template
    (path / "2_b.down.sql").write_text("SELECT 5;", encoding="utf-8")
    assert name_of(path) == with_up
    # the history table and the schemas created first are part of the template too
    others = {name_of(path, table="app.history"), name_of(path, create_schemas=["app"])}
    assert len(others) == 2
    assert with_up not in others
    assert name_of(path, create_schemas=["b", "a"]) == name_of(path, create_schemas=["a", "b", "a"])
    (path / "1_a.sql").write_text("SELECT 4;", encoding="utf-8")
    assert name_of(path) not in {first, with_up}


def test_an_empty_template_is_named_by_the_directory_alone(tmp_path: Path) -> None:
    first = template_name(None, directory=tmp_path)
    assert first.startswith(f"{TEMPLATE_PREFIX}{project_prefix(tmp_path)}_")
    assert template_name(None, directory=tmp_path, rowx="9.9.9") != first


@dataclass(slots=True, frozen=True)
class FixedMigration:
    version: int
    checksum: bytes


@dataclass(slots=True, frozen=True)
class FixedSource:
    migrations: tuple[FixedMigration, ...]


# computed with `template_name` as it is on main, before it took an identity: a server template keeps its name
def test_a_template_name_without_an_identity_is_what_main_computes() -> None:
    source = FixedSource((FixedMigration(1, bytes.fromhex("aa" * 32)), FixedMigration(2, bytes.fromhex("bb" * 32))))
    directory = Path("/nonexistent/rowx-golden/migrations")
    assert template_name(source, directory=directory, rowx="1.0.0") == "rowx_tpl_5fe1e6ac_0fb702728f03be3c"
    assert (
        template_name(source, directory=directory, rowx="1.0.0", table="app.history", create_schemas=["b", "a"])
        == "rowx_tpl_5fe1e6ac_e26b99e4ded92686"
    )
    assert template_name(None, directory=directory, rowx="1.0.0") == "rowx_tpl_5fe1e6ac_d24aa09b4c0bbd3d"
    assert template_name(source, directory=directory, rowx="1.0.0", identity=()) == template_name(
        source, directory=directory, rowx="1.0.0"
    )


def test_template_name_changes_with_the_backend_identity(tmp_path: Path) -> None:
    path = migrations(tmp_path / "migrations")
    base = ("rowx-embedded:18.4.0", "snapshot-format:1", "extensions:citext,hstore")
    first = template_name(source(path), directory=path, rowx="1.0.0", identity=base)
    assert template_name(source(path), directory=path, rowx="1.0.0", identity=base) == first
    assert first != template_name(source(path), directory=path, rowx="1.0.0")
    for changed in (
        ("rowx-embedded:18.4.1", *base[1:]),
        (base[0], "snapshot-format:2", base[2]),
        (*base[:2], "extensions:citext"),
    ):
        other = template_name(source(path), directory=path, rowx="1.0.0", identity=changed)
        assert other != first
        # `rowx_tpl_<p>_<h>`: the project prefix `<p>` stays, only the hash `<h>` changes
        assert other.split("_")[2] == first.split("_")[2]
        assert other.split("_")[3] != first.split("_")[3]


def test_blocking_returns_the_result_of_the_work() -> None:
    async def work() -> int:
        await asyncio.sleep(0)
        return 7

    assert blocking(work()) == 7


def test_an_interrupt_cancels_the_work_and_runs_its_cleanup_before_returning_control() -> None:
    running = threading.Event()
    outcome: list[str] = []

    async def work() -> None:
        running.set()
        try:
            await asyncio.sleep(LONG_WAIT)
            outcome.append("finished")
        except asyncio.CancelledError:
            outcome.append("cancelled")
            raise

    def interrupt() -> None:
        assert running.wait(LONG_WAIT)
        time.sleep(INTERRUPT_DELAY)
        # a signal aimed at the main thread, which is blocked waiting for the work
        signal.pthread_kill(threading.main_thread().ident or 0, signal.SIGINT)

    threading.Thread(target=interrupt, daemon=True).start()
    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        blocking(work())
    assert outcome == ["cancelled"]
    assert time.monotonic() - started < LONG_WAIT / 2
