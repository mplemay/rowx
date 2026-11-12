import asyncio
import hashlib
import os
import secrets
from typing import TYPE_CHECKING, Final

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from rowx_core.errors import (
    ConnectionBusyError,
    DatabaseError,
    DirtyMigrationError,
    InterfaceError,
    IrreversibleMigrationError,
    MigrationChecksumError,
    MigrationFailedError,
    MigrationMissingError,
    MigrationOrderError,
    MigrationSourceError,
)
from rowx_postgres import Connection, MigrationResult, Migrator, Pool, connect

from rowx import query, query_unchecked

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator
    from pathlib import Path

pytestmark = [pytest.mark.db]

POLL_LIMIT: Final[int] = 5000
HISTORY: Final[str] = "_rowx_migrations"


def write(directory: Path, name: str, text: str) -> None:
    (directory / name).write_text(text, encoding="utf-8")


def reversible(directory: Path, version: int, table: str) -> None:
    write(directory, f"{version}_{table}.up.sql", f"CREATE TABLE {table} (id int4);")
    write(directory, f"{version}_{table}.down.sql", f"DROP TABLE {table};")


async def table_exists(conn: Connection | Pool, name: str) -> bool:
    return await query[bool](t"SELECT to_regclass({name}) IS NOT NULL").one(conn)


async def history(conn: Connection | Pool) -> list[tuple[int, bool]]:
    return await query_unchecked[tuple[int, bool]](
        t"SELECT version, success FROM _rowx_migrations ORDER BY version"
    ).all(
        conn,
    )


async def advisory_locks(observer: Connection | Pool, pid: int) -> int:
    return await query[int](t"SELECT count(*)::int4 FROM pg_locks WHERE locktype = 'advisory' AND pid = {pid}").one(
        observer,
    )


async def backend_pid(conn: Connection | Pool) -> int:
    return await query[int](t"SELECT pg_backend_pid()").one(conn)


def test_run_on_a_connection_applies_in_version_order(schema: str, tmp_path: Path) -> None:
    write(tmp_path, "2_second.sql", "CREATE TABLE second (id int4);")
    write(tmp_path, "1_first.sql", "CREATE TABLE first (id int4);")
    migrator = Migrator(tmp_path)

    async def run() -> None:
        async with await connect(schema) as conn:
            applied = await migrator.run(conn)
            assert [(r.migration.version, r.migration.description) for r in applied] == [
                (1, "first"),
                (2, "second"),
            ]
            assert all(isinstance(r, MigrationResult) for r in applied)
            # the time is written after the commit, so it is a real duration
            assert all(r.elapsed is not None and r.elapsed.total_seconds() > 0 for r in applied)
            assert await table_exists(conn, "first")
            assert await table_exists(conn, "second")
            assert await history(conn) == [(1, True), (2, True)]
            assert await migrator.run(conn) == ()
            # the lock is released and the connection is usable
            assert await advisory_locks(conn, await backend_pid(conn)) == 0

    asyncio.run(run())


def test_run_reports_from_the_history_table_it_resolved_before_the_files(schema: str, tmp_path: Path) -> None:
    # a plain SET outlives the migration's transaction, so an unqualified name would now miss the table
    write(tmp_path, "1_move.sql", "SET search_path TO pg_catalog;")
    migrator = Migrator(tmp_path)

    async def run() -> None:
        async with await connect(schema) as conn:
            applied = await migrator.run(conn)
            assert [r.migration.version for r in applied] == [1]
            assert applied[0].elapsed is not None

    asyncio.run(run())


def test_run_on_a_pool_releases_the_connection_and_the_lock(schema: str, tmp_path: Path) -> None:
    write(tmp_path, "1_first.sql", "CREATE TABLE first (id int4);")
    migrator = Migrator(tmp_path)

    async def run() -> None:
        async with Pool(schema, max_size=1, acquire_timeout=10) as pool:
            pid = await backend_pid(pool)
            applied = await migrator.run(pool)
            assert [r.migration.version for r in applied] == [1]
            # with a single connection, this only succeeds when the migrator gave it back
            assert await table_exists(pool, "first")
            assert await backend_pid(pool) == pid
            assert await advisory_locks(pool, pid) == 0
            assert pool.size == 1

    asyncio.run(run())


def test_run_on_a_pool_with_hooks_checks_the_connection_out_in_python(schema: str, tmp_path: Path) -> None:
    write(tmp_path, "1_first.sql", "CREATE TABLE first (id int4);")
    migrator = Migrator(tmp_path)
    seen: list[str] = []

    async def setup(conn: Connection) -> None:
        seen.append("setup")
        await conn.ping()

    async def run() -> None:
        async with Pool(schema, max_size=1, setup=setup) as pool:
            await migrator.run(pool)
            assert seen == ["setup"]
            assert await table_exists(pool, "first")

    asyncio.run(run())


def test_run_stops_at_a_target(schema: str, tmp_path: Path) -> None:
    for version in (1, 2, 3):
        write(tmp_path, f"{version}_t{version}.sql", f"CREATE TABLE t{version} (id int4);")
    migrator = Migrator(tmp_path)

    async def run() -> None:
        async with await connect(schema) as conn:
            assert [r.migration.version for r in await migrator.run(conn, target=2)] == [1, 2]
            assert not await table_exists(conn, "t3")
            assert [r.migration.version for r in await migrator.run(conn)] == [3]

    asyncio.run(run())


def test_revert_reverts_newest_first_to_a_target_and_everything(schema: str, tmp_path: Path) -> None:
    for version in (1, 2, 3):
        reversible(tmp_path, version, f"t{version}")
    migrator = Migrator(tmp_path)

    async def run() -> None:
        async with await connect(schema) as conn:
            await migrator.run(conn)
            reverted = await migrator.revert(conn, target=1)
            assert [r.migration.version for r in reverted] == [3, 2]
            assert all(r.elapsed is not None for r in reverted)
            assert await table_exists(conn, "t1")
            assert not await table_exists(conn, "t2")
            assert await history(conn) == [(1, True)]
            assert [r.migration.version for r in await migrator.revert(conn, all=True)] == [1]
            assert not await table_exists(conn, "t1")
            assert await migrator.revert(conn, all=True) == ()
            # reverted migrations apply again
            assert [r.migration.version for r in await migrator.run(conn)] == [1, 2, 3]

    asyncio.run(run())


def test_revert_without_a_target_reverts_the_latest_only_and_times_it(schema: str, tmp_path: Path) -> None:
    for version in (1, 5):
        reversible(tmp_path, version, f"t{version}")
    migrator = Migrator(tmp_path)

    async def run() -> None:
        async with await connect(schema) as conn:
            await migrator.run(conn)
            (reverted,) = await migrator.revert(conn)
            assert (reverted.migration.version, reverted.migration.description) == (5, "t5")
            assert reverted.elapsed is not None
            assert reverted.elapsed.total_seconds() > 0
            assert await history(conn) == [(1, True)]
            assert [r.migration.version for r in await migrator.revert(conn)] == [1]
            assert await migrator.revert(conn) == ()

    asyncio.run(run())


def test_a_dry_run_returns_the_planned_statuses_and_changes_nothing(schema: str, tmp_path: Path) -> None:
    for version in (1, 2, 3):
        reversible(tmp_path, version, f"t{version}")
    migrator = Migrator(tmp_path)
    # only ever dry-run, so the schema is never created
    never = f"mig_dry_{secrets.token_hex(4)}"
    dry = Migrator(tmp_path, table=f"{never}.history", create_schemas=[never])

    async def run() -> None:
        async with await connect(schema) as conn:
            assert [r.migration.version for r in await dry.run(conn, dry_run=True)] == [1, 2, 3]
            assert await dry.revert(conn, dry_run=True) == ()
            assert not await query[bool](t"SELECT EXISTS (SELECT FROM pg_namespace WHERE nspname = {never})").one(conn)

            planned = await migrator.run(conn, target=2, dry_run=True)
            assert [(r.migration.version, r.migration.description) for r in planned] == [(1, "t1"), (2, "t2")]
            assert all(r.elapsed is None for r in planned)
            assert await migrator.revert(conn, all=True, dry_run=True) == ()
            assert not await table_exists(conn, HISTORY)
            assert not await table_exists(conn, "t1")

            await migrator.run(conn, target=2)
            assert [r.migration.version for r in await migrator.run(conn, dry_run=True)] == [3]
            reverting = await migrator.revert(conn, all=True, dry_run=True)
            assert [r.migration.version for r in reverting] == [2, 1]
            assert all(r.elapsed is None for r in reverting)
            assert [r.migration.version for r in await migrator.revert(conn, dry_run=True)] == [2]
            assert await history(conn) == [(1, True), (2, True)]
            assert await table_exists(conn, "t2")

            # a dry run raises the real run's errors, in the same order
            with pytest.raises(MigrationOrderError):
                await migrator.run(conn, target=1, dry_run=True)
            # the same up file as a simple migration has the same checksum, but no .down.sql
            (tmp_path / "1_t1.down.sql").unlink()
            (tmp_path / "1_t1.up.sql").rename(tmp_path / "1_t1.sql")
            with pytest.raises(IrreversibleMigrationError) as exc:
                await Migrator(tmp_path).revert(conn, all=True, dry_run=True)
            assert exc.value.version == 1
            assert await history(conn) == [(1, True), (2, True)]

    asyncio.run(run())


def test_revert_without_a_down_file_raises_before_reverting_anything(schema: str, tmp_path: Path) -> None:
    reversible(tmp_path, 1, "t1")
    write(tmp_path, "2_t2.sql", "CREATE TABLE t2 (id int4);")
    migrator = Migrator(tmp_path)

    async def run() -> None:
        async with await connect(schema) as conn:
            await migrator.run(conn)
            with pytest.raises(IrreversibleMigrationError) as exc:
                await migrator.revert(conn, all=True)
            assert exc.value.version == 2
            assert isinstance(exc.value, MigrationSourceError)
            assert await history(conn) == [(1, True), (2, True)]
            assert await table_exists(conn, "t1")

    asyncio.run(run())


def test_skip_records_migrations_without_running_them(schema: str, tmp_path: Path) -> None:
    for version in (1, 2, 3):
        write(tmp_path, f"{version}_t{version}.sql", f"CREATE TABLE t{version} (id int4);")
    migrator = Migrator(tmp_path)

    async def run() -> None:
        async with await connect(schema) as conn:
            skipped = await migrator.skip(conn, target=2)
            assert [r.migration.version for r in skipped] == [1, 2]
            assert all(r.elapsed is None for r in skipped)
            assert [s.state for s in await migrator.info(conn)] == ["applied", "applied", "pending"]
            assert not await table_exists(conn, "t1")
            assert [r.migration.version for r in await migrator.run(conn)] == [3]
            assert await table_exists(conn, "t3")
            assert not await table_exists(conn, "t2")

    asyncio.run(run())


def test_info_reports_every_state_and_creates_nothing(schema: str, tmp_path: Path) -> None:
    write(tmp_path, "1_one.sql", "CREATE TABLE t1 (id int4);")
    write(tmp_path, "2_two.sql", "CREATE TABLE t2 (id int4);")
    migrator = Migrator(tmp_path)

    async def run() -> None:
        async with await connect(schema) as conn:
            before = await migrator.info(conn)
            assert [(s.version, s.state, s.installed_on) for s in before] == [
                (1, "pending", None),
                (2, "pending", None),
            ]
            assert not await table_exists(conn, HISTORY)

            await migrator.run(conn)
            write(tmp_path, "3_three.sql", "-- no-transaction\nSELECT 1 / 0;")
            # the dirty row of 3 is written before its file fails
            with pytest.raises(MigrationFailedError):
                await Migrator(tmp_path).run(conn)
            write(tmp_path, "2_two.sql", "CREATE TABLE t2 (id int8);")
            write(tmp_path, "4_four.sql", "SELECT 1;")
            await query(
                t"INSERT INTO _rowx_migrations (version, description, success, checksum, execution_time) VALUES (5, 'gone', true, {b'x'}, 1)",
            ).execute(conn)

            states = {s.version: s for s in await Migrator(tmp_path).info(conn)}
            assert {v: s.state for v, s in states.items()} == {
                1: "applied",
                2: "modified",
                3: "dirty",
                4: "pending",
                5: "missing",
            }
            assert states[1].installed_on is not None
            assert states[1].execution_time is not None
            assert states[1].kind == "simple"
            assert states[1].local_checksum == states[1].applied_checksum
            # a modified row has both checksums, and they differ
            assert states[2].local_checksum == hashlib.sha384(b"CREATE TABLE t2 (id int8);").digest()
            assert states[2].applied_checksum == hashlib.sha384(b"CREATE TABLE t2 (id int4);").digest()
            assert states[4].local_checksum is not None
            assert states[4].applied_checksum is None
            # a missing migration has no local file
            assert (states[5].kind, states[5].local_checksum, states[5].applied_checksum) == (None, None, b"x")
            assert states[4].installed_on is None
            assert states[3].description == "three"
            assert not states[5].description

    asyncio.run(run())


def test_a_failing_file_is_a_failed_error_caused_by_the_database_error(schema: str, tmp_path: Path) -> None:
    write(tmp_path, "1_ok.sql", "CREATE TABLE ok (id int4);")
    write(tmp_path, "2_bad.sql", "CREATE TABLE half (id int4); SELECT 1 / 0;")
    migrator = Migrator(tmp_path)

    async def run() -> None:
        async with await connect(schema) as conn:
            with pytest.raises(MigrationFailedError) as exc:
                await migrator.run(conn)
            assert exc.value.version == 2
            assert isinstance(exc.value.__cause__, DatabaseError)
            assert exc.value.__cause__.sqlstate == "22012"
            assert exc.value.error is exc.value.__cause__
            assert exc.value.error.sqlstate == "22012"
            assert exc.value.path == tmp_path / "2_bad.sql"
            assert exc.value.dirty is False
            # what completed before the failure
            assert [(r.migration.version, r.elapsed is not None) for r in exc.value.applied] == [(1, True)]
            # a transactional file leaves nothing behind, and the earlier migration stays
            assert await history(conn) == [(1, True)]
            assert not await table_exists(conn, "half")
            assert await advisory_locks(conn, await backend_pid(conn)) == 0
            # the connection is still usable
            assert await query[int](t"SELECT 1").one(conn) == 1

    asyncio.run(run())


def test_a_failed_no_transaction_file_leaves_a_dirty_row(schema: str, tmp_path: Path) -> None:
    write(tmp_path, "1_bad.sql", "-- no-transaction\nSELECT 1 / 0;")
    migrator = Migrator(tmp_path)

    async def run() -> None:
        async with await connect(schema) as conn:
            with pytest.raises(MigrationFailedError) as failed:
                await migrator.run(conn)
            assert failed.value.version == 1
            assert failed.value.dirty is True
            assert failed.value.applied == ()
            assert failed.value.error.sqlstate == "22012"
            assert await history(conn) == [(1, False)]
            with pytest.raises(DirtyMigrationError) as dirty:
                await migrator.run(conn)
            assert dirty.value.version == 1
            assert [s.state for s in await migrator.info(conn)] == ["dirty"]

    asyncio.run(run())


def test_create_index_concurrently_needs_to_be_the_only_statement(schema: str, tmp_path: Path) -> None:
    write(tmp_path, "1_t.sql", "CREATE TABLE t (id int4);")
    write(tmp_path, "2_index.sql", "-- no-transaction\nCREATE INDEX CONCURRENTLY t_id ON t (id);")
    write(tmp_path, "3_bad.sql", "-- no-transaction\nSELECT 1; CREATE INDEX CONCURRENTLY t_id2 ON t (id);")
    migrator = Migrator(tmp_path)

    async def run() -> None:
        async with await connect(schema) as conn:
            with pytest.raises(MigrationFailedError) as exc:
                await migrator.run(conn)
            assert exc.value.version == 3
            assert isinstance(exc.value.__cause__, DatabaseError)
            assert exc.value.__cause__.sqlstate == "25001"
            assert exc.value.dirty is True
            assert [r.migration.version for r in exc.value.applied] == [1, 2]
            assert await history(conn) == [(1, True), (2, True), (3, False)]

    asyncio.run(run())


def test_a_modified_file_is_a_checksum_error(schema: str, tmp_path: Path) -> None:
    write(tmp_path, "1_t.sql", "CREATE TABLE t (id int4);")
    migrator = Migrator(tmp_path)

    async def run() -> None:
        async with await connect(schema) as conn:
            await migrator.run(conn)
            write(tmp_path, "1_t.sql", "CREATE TABLE t (id int8);")
            with pytest.raises(MigrationChecksumError) as exc:
                await Migrator(tmp_path).run(conn)
            assert exc.value.version == 1
            assert exc.value.local_checksum == hashlib.sha384(b"CREATE TABLE t (id int8);").digest()
            assert exc.value.applied_checksum == hashlib.sha384(b"CREATE TABLE t (id int4);").digest()

    asyncio.run(run())


def test_a_missing_file_is_a_missing_error_unless_ignored(schema: str, tmp_path: Path) -> None:
    write(tmp_path, "1_t.sql", "CREATE TABLE t (id int4);")
    write(tmp_path, "2_u.sql", "CREATE TABLE u (id int4);")

    async def run() -> None:
        async with await connect(schema) as conn:
            await Migrator(tmp_path).run(conn)
            (tmp_path / "2_u.sql").unlink()
            write(tmp_path, "3_v.sql", "CREATE TABLE v (id int4);")
            with pytest.raises(MigrationMissingError) as exc:
                await Migrator(tmp_path).run(conn)
            assert exc.value.versions == (2,)
            assert [r.migration.version for r in await Migrator(tmp_path, ignore_missing=True).run(conn)] == [3]

    asyncio.run(run())


def test_out_of_order_is_an_error_unless_allowed(schema: str, tmp_path: Path) -> None:
    write(tmp_path, "2_b.sql", "CREATE TABLE b (id int4);")

    async def run() -> None:
        async with await connect(schema) as conn:
            await Migrator(tmp_path).run(conn)
            write(tmp_path, "1_a.sql", "CREATE TABLE a (id int4);")
            with pytest.raises(MigrationOrderError) as exc:
                await Migrator(tmp_path).run(conn)
            assert (exc.value.version, exc.value.latest) == (1, 2)
            assert not await table_exists(conn, "a")
            assert [r.migration.version for r in await Migrator(tmp_path, allow_out_of_order=True).run(conn)] == [1]

    asyncio.run(run())


def test_an_unknown_target_is_a_value_error_and_one_below_the_latest_an_order_error(
    schema: str, tmp_path: Path
) -> None:
    write(tmp_path, "1_a.sql", "CREATE TABLE a (id int4);")
    write(tmp_path, "2_b.sql", "CREATE TABLE b (id int4);")
    migrator = Migrator(tmp_path)

    async def run() -> None:
        async with await connect(schema) as conn:
            with pytest.raises(ValueError, match="99"):
                await migrator.run(conn, target=99)
            # before any IO: nothing was created
            assert not await table_exists(conn, HISTORY)
            await migrator.run(conn)
            with pytest.raises(MigrationOrderError):
                await migrator.run(conn, target=1)

    asyncio.run(run())


def test_a_schema_qualified_table_and_create_schemas(schema: str, tmp_path: Path) -> None:
    write(tmp_path, "1_t.sql", "CREATE TABLE t (id int4);")
    migrator = Migrator(tmp_path, table="mig_app._rowx_migrations", create_schemas=["mig_app"])

    async def run() -> None:
        async with await connect(schema) as conn:
            await migrator.run(conn)
            assert await table_exists(conn, "mig_app._rowx_migrations")
            assert [s.state for s in await migrator.info(conn)] == ["applied"]
            await query(t"DROP SCHEMA mig_app CASCADE").execute(conn)

    asyncio.run(run())


def test_a_transaction_argument_is_an_interface_error(schema: str, tmp_path: Path) -> None:
    write(tmp_path, "1_t.sql", "CREATE TABLE t (id int4);")
    migrator = Migrator(tmp_path)

    async def run() -> None:
        async with await connect(schema) as conn, conn.transaction() as tx:
            for call in (migrator.run(tx), migrator.revert(tx), migrator.skip(tx), migrator.info(tx)):  # ty: ignore[invalid-argument-type]
                with pytest.raises(InterfaceError, match="Transaction"):
                    await call
            # a pool transaction is a transaction too
        async with Pool(schema) as pool, pool.transaction() as pool_tx:
            with pytest.raises(InterfaceError):
                await migrator.run(pool_tx)  # ty: ignore[invalid-argument-type]
        async with await connect(schema) as conn:
            assert not await table_exists(conn, "t")

    asyncio.run(run())


def test_a_connection_inside_a_transaction_is_an_interface_error(schema: str, tmp_path: Path) -> None:
    migrator = Migrator(tmp_path)

    async def run() -> None:
        async with await connect(schema) as conn, conn.transaction():
            with pytest.raises(InterfaceError):
                await migrator.info(conn)

    asyncio.run(run())


@pytest.mark.timing
def test_a_second_operation_on_a_busy_connection_is_a_busy_error(schema: str, tmp_path: Path) -> None:
    write(tmp_path, "1_slow.sql", "SELECT pg_sleep(30);")
    migrator = Migrator(tmp_path)

    async def run() -> None:
        async with await connect(schema) as conn, await connect(schema) as observer:
            pid = await backend_pid(conn)
            first = asyncio.ensure_future(migrator.run(conn))
            # the first operation holds the connection for as long as its statement runs
            await wait_for_statement(observer, [pid], "%pg_sleep(30)%")
            with pytest.raises(ConnectionBusyError):
                await migrator.info(conn)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first

    asyncio.run(run())


def test_a_closed_pool_is_an_interface_error(schema: str, tmp_path: Path) -> None:
    migrator = Migrator(tmp_path)

    async def run() -> None:
        pool = Pool(schema)
        await pool.open()
        await pool.close()
        with pytest.raises(InterfaceError):
            await migrator.info(pool)

    asyncio.run(run())


@pytest.mark.server(needs="sessions")
def test_a_run_on_a_pool_makes_other_connections_reprepare_stale_plans(schema: str, tmp_path: Path) -> None:
    write(tmp_path, "1_create.sql", "CREATE TABLE epoch_t (a int4); INSERT INTO epoch_t VALUES (1);")
    migrator = Migrator(tmp_path)

    async def run() -> list[int]:
        table = query_unchecked[int](t"SELECT a FROM epoch_t")
        async with Pool(schema, max_size=2) as pool:
            await migrator.run(pool)
            # one cached plan on each of the two connections
            async with pool.acquire() as first, pool.acquire() as second:
                await table.one(first)
                await table.one(second)
            write(tmp_path, "2_alter.sql", "ALTER TABLE epoch_t ALTER COLUMN a TYPE int8;")
            await Migrator(tmp_path).run(pool)
            # inside a transaction a stale plan cannot be retried, so this only works after the epoch bump
            results: list[int] = []
            async with pool.acquire() as first, pool.acquire() as second:
                for conn in (first, second):
                    await query(t"BEGIN").execute(conn)
                    results.append(await table.one(conn))
            return results

    assert asyncio.run(run()) == [1, 1]


@pytest.mark.timing
def test_a_cancelled_run_leaves_no_lock_and_no_row(schema: str, tmp_path: Path) -> None:
    write(tmp_path, "1_slow.sql", "SELECT pg_sleep(30);")
    migrator = Migrator(tmp_path)

    async def wait_for_sleep(observer: Connection, pid: int) -> None:
        for _ in range(POLL_LIMIT):
            active = await query[bool](
                t"SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE pid = {pid} AND state = 'active' AND query LIKE '%pg_sleep(30)%')",
            ).one(observer)
            if active:
                return
        pytest.fail("the migration never started")

    async def wait_for_no_lock(observer: Connection, pid: int) -> None:
        for _ in range(POLL_LIMIT):
            if await advisory_locks(observer, pid) == 0:
                return
        pytest.fail("the advisory lock was never released")

    async def run() -> None:
        async with Pool(schema, max_size=1, acquire_timeout=10) as pool, await connect(schema) as observer:
            pid = await backend_pid(pool)
            task = asyncio.ensure_future(migrator.run(pool))
            await wait_for_sleep(observer, pid)
            assert await advisory_locks(observer, pid) == 1
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await wait_for_no_lock(observer, pid)
            # the connection is usable again and the transactional migration was not recorded
            assert await query[int](t"SELECT 1").one(pool) == 1
            assert await history(pool) == []

    asyncio.run(run())


@pytest.mark.timing
def test_a_cancelled_no_transaction_run_leaves_a_dirty_row(schema: str, tmp_path: Path) -> None:
    write(tmp_path, "1_slow.sql", "-- no-transaction\nSELECT pg_sleep(30);")
    migrator = Migrator(tmp_path)

    async def run() -> None:
        async with await connect(schema) as conn, await connect(schema) as observer:
            pid = await backend_pid(conn)
            task = asyncio.ensure_future(migrator.run(conn))
            for _ in range(POLL_LIMIT):
                if await query[bool](
                    t"SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE pid = {pid} AND state = 'active' AND query LIKE '%pg_sleep(30)%')",
                ).one(observer):
                    break
            else:
                pytest.fail("the migration never started")
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert [s.state for s in await migrator.info(conn)] == ["dirty"]

    asyncio.run(run())


@pytest.mark.timing
def test_a_cancelled_run_on_a_pool_still_drops_stale_plans(schema: str, tmp_path: Path) -> None:
    write(tmp_path, "1_create.sql", "CREATE TABLE epoch_t (a int4); INSERT INTO epoch_t VALUES (1);")

    async def run() -> list[int]:
        table = query_unchecked[int](t"SELECT a FROM epoch_t")
        async with Pool(schema, max_size=2) as pool, await connect(schema) as observer:
            await Migrator(tmp_path).run(pool)
            async with pool.acquire() as first, pool.acquire() as second:
                await table.one(first)
                await table.one(second)
                pids = [await backend_pid(first), await backend_pid(second)]
            # the change commits, then the run is cancelled during the next file
            write(tmp_path, "2_alter.sql", "ALTER TABLE epoch_t ALTER COLUMN a TYPE int8;")
            write(tmp_path, "3_slow.sql", "SELECT pg_sleep(30);")
            task = asyncio.ensure_future(Migrator(tmp_path).run(pool))
            for _ in range(POLL_LIMIT):
                if await query[bool](
                    t"SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE pid = ANY({pids}) AND state = 'active' AND query LIKE '%pg_sleep(30)%')",
                ).one(observer):
                    break
            else:
                pytest.fail("the migration never started")
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            # the migrating connection is idle again only once its task has settled
            for _ in range(POLL_LIMIT):
                if pool.idle == 2:
                    break
                await query[int](t"SELECT 1").one(observer)
            else:
                pytest.fail("the migrating connection never came back")
            results: list[int] = []
            async with pool.acquire() as first, pool.acquire() as second:
                for conn in (first, second):
                    await query(t"BEGIN").execute(conn)
                    results.append(await table.one(conn))
            return results

    assert asyncio.run(run()) == [1, 1]


@pytest.mark.server(needs="sessions")
def test_two_migrators_on_one_database_both_finish(schema: str, tmp_path: Path) -> None:
    write(tmp_path, "1_t.sql", "CREATE TABLE t (id int4);")
    migrator = Migrator(tmp_path)

    async def run() -> list[int]:
        async with Pool(schema, max_size=2) as pool:
            results = await asyncio.gather(migrator.run(pool), migrator.run(pool))
            return sorted(len(r) for r in results)

    # one applied it, the other waited on the lock and found nothing to do
    assert asyncio.run(run()) == [0, 1]


async def wait_for_statement(observer: Connection, pids: list[int], needle: str) -> None:
    for _ in range(POLL_LIMIT):
        if await query[bool](
            t"SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE pid = ANY({pids}) AND state = 'active' AND query LIKE {needle})",
        ).one(observer):
            return
    pytest.fail("the migration never started")


async def wait_for_no_lock(observer: Connection, pid: int) -> None:
    for _ in range(POLL_LIMIT):
        if await advisory_locks(observer, pid) == 0:
            return
    pytest.fail("the advisory lock was never released")


@pytest.mark.timing
def test_a_cancelled_transactional_run_on_a_connection_leaves_no_row_and_no_lock(
    schema: str,
    tmp_path: Path,
) -> None:
    write(tmp_path, "1_slow.sql", "CREATE TABLE cancelled_conn (id int4); SELECT pg_sleep(31);")
    migrator = Migrator(tmp_path)

    async def run() -> None:
        async with await connect(schema) as conn, await connect(schema) as observer:
            pid = await backend_pid(conn)
            task = asyncio.ensure_future(migrator.run(conn))
            await wait_for_statement(observer, [pid], "%pg_sleep(31)%")
            assert await advisory_locks(observer, pid) == 1
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            # the next query waits for the cancelled run to settle, and then succeeds
            assert await query[int](t"SELECT 1").one(conn) == 1
            assert await advisory_locks(observer, pid) == 0
            assert await history(conn) == []
            assert not await table_exists(conn, "cancelled_conn")

    asyncio.run(run())


@pytest.mark.timing
def test_a_cancel_during_the_first_migration_stops_the_second_from_running(schema: str, tmp_path: Path) -> None:
    # the first file swallows the cancel, so only the executor's check keeps the second from starting
    write(
        tmp_path,
        "1_stubborn.sql",
        "DO $$ BEGIN PERFORM pg_sleep(32); EXCEPTION WHEN query_canceled THEN NULL; END $$;",
    )
    write(tmp_path, "2_second.sql", "CREATE TABLE second_never (id int4);")
    migrator = Migrator(tmp_path)

    async def run() -> None:
        async with await connect(schema) as conn, await connect(schema) as observer:
            pid = await backend_pid(conn)
            task = asyncio.ensure_future(migrator.run(conn))
            await wait_for_statement(observer, [pid], "%pg_sleep(32)%")
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert await query[int](t"SELECT 1").one(conn) == 1
            assert await advisory_locks(observer, pid) == 0
            assert 2 not in [version for version, _ in await history(conn)]
            assert not await table_exists(conn, "second_never")

    asyncio.run(run())


@pytest.mark.timing
def test_a_cancelled_run_on_a_pool_without_a_reset_query_leaves_no_lock(schema: str, tmp_path: Path) -> None:
    write(tmp_path, "1_slow.sql", "SELECT pg_sleep(33);")
    migrator = Migrator(tmp_path)

    async def run() -> None:
        async with (
            Pool(schema, max_size=1, acquire_timeout=10, reset_query=None) as pool,
            await connect(schema) as observer,
        ):
            pid = await backend_pid(pool)
            task = asyncio.ensure_future(migrator.run(pool))
            await wait_for_statement(observer, [pid], "%pg_sleep(33)%")
            assert await advisory_locks(observer, pid) == 1
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await wait_for_no_lock(observer, pid)
            # whatever the pool kept or discarded, a connection from it holds no migrator lock
            new_pid = await backend_pid(pool)
            assert await advisory_locks(observer, new_pid) == 0
            assert await history(pool) == []

    asyncio.run(run())


@pytest.mark.server(needs="sessions")
def test_a_failed_unlock_discards_the_pooled_connection(schema: str, tmp_path: Path) -> None:
    # the file terminates its own backend, so neither the rollback nor the unlock can run
    write(tmp_path, "1_kill.sql", "SELECT pg_terminate_backend(pg_backend_pid()); SELECT pg_sleep(34);")
    migrator = Migrator(tmp_path)

    async def run() -> None:
        async with (
            Pool(schema, max_size=1, acquire_timeout=10, reset_query=None) as pool,
            await connect(schema) as observer,
        ):
            pid = await backend_pid(pool)
            with pytest.raises((MigrationFailedError, DatabaseError, InterfaceError)):
                await migrator.run(pool)
            new_pid = await backend_pid(pool)
            assert new_pid != pid
            assert await advisory_locks(observer, pid) == 0
            assert pool.size == 1

    asyncio.run(run())


# a database of its own, for a file that changes what the whole database allows
@pytest.fixture
def database() -> Iterator[str]:
    if (server := os.environ.get("ROWX_TEST_DSN")) is None:
        if os.environ.get("ROWX_REQUIRE_DB") == "1":
            pytest.fail("ROWX_REQUIRE_DB=1 but ROWX_TEST_DSN is not set")
        pytest.skip("ROWX_TEST_DSN is not set")
    name = f"rowx_migrate_{secrets.token_hex(6)}"
    with psycopg.connect(server, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield make_conninfo(server, dbname=name)
    finally:
        with psycopg.connect(server, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


async def ping(conn: Connection) -> None:
    await conn.ping()


@pytest.mark.parametrize("setup", [None, ping], ids=["in_rust", "with_hooks"])
@pytest.mark.server(needs="create-database")
def test_a_failed_unlock_on_a_live_connection_discards_the_pooled_connection(
    database: str,
    tmp_path: Path,
    setup: Callable[[Connection], Awaitable[None]] | None,
) -> None:
    # the file commits a revoke and leaves the session as a role that may neither update the history
    # table nor unlock, so the session stays alive holding the lock; with `reset_query=None` only the
    # broken mark keeps the pool from handing it out again
    write(
        tmp_path,
        "1_revoke.sql",
        "-- no-transaction\nREVOKE EXECUTE ON FUNCTION pg_catalog.pg_advisory_unlock(bigint) FROM PUBLIC;\n"
        "SET ROLE pg_monitor;\n",
    )
    migrator = Migrator(tmp_path)

    async def run() -> None:
        async with (
            Pool(database, max_size=1, acquire_timeout=10, reset_query=None, setup=setup) as pool,
            await connect(database) as observer,
        ):
            pid = await backend_pid(pool)
            with pytest.raises(DatabaseError, match="permission denied"):
                await migrator.run(pool)
            assert await backend_pid(pool) != pid
            await wait_for_no_lock(observer, pid)

    asyncio.run(run())


def test_a_file_ending_in_rollback_fails_and_records_nothing(schema: str, tmp_path: Path) -> None:
    write(tmp_path, "1_rollback.sql", "CREATE TABLE ended_rollback (id int4); ROLLBACK;")
    migrator = Migrator(tmp_path)

    async def run() -> None:
        async with await connect(schema) as conn:
            with pytest.raises(MigrationFailedError, match="ended rowx's transaction"):
                await migrator.run(conn)
            assert await history(conn) == []
            assert not await table_exists(conn, "ended_rollback")
            assert await query[int](t"SELECT 1").one(conn) == 1

    asyncio.run(run())


def test_a_file_ending_in_commit_fails_but_its_changes_stay_committed(schema: str, tmp_path: Path) -> None:
    write(tmp_path, "1_commit.sql", "CREATE TABLE ended_commit (id int4); COMMIT;")
    migrator = Migrator(tmp_path)

    async def run() -> None:
        async with await connect(schema) as conn:
            with pytest.raises(MigrationFailedError, match="stays committed"):
                await migrator.run(conn)
            # the file committed its own DDL before the probe, so it stays, unrecorded
            assert await history(conn) == []
            assert await table_exists(conn, "ended_commit")

    asyncio.run(run())


def test_revert_rejects_a_target_with_all_and_a_bad_target(schema: str, tmp_path: Path) -> None:
    for version in (1, 2):
        reversible(tmp_path, version, f"t{version}")
    migrator = Migrator(tmp_path)

    async def run() -> None:
        async with await connect(schema) as conn:
            with pytest.raises(ValueError, match="target"):
                await migrator.revert(conn, target=1, all=True)
            # an unknown target fails before any IO, so the history table isn't created
            with pytest.raises(ValueError, match="99"):
                await migrator.revert(conn, target=99)
            assert not await table_exists(conn, HISTORY)
            await migrator.run(conn, target=1)
            # a target above the latest applied version
            with pytest.raises(ValueError, match="newer"):
                await migrator.revert(conn, target=2)
            assert await history(conn) == [(1, True)]

    asyncio.run(run())


def test_a_dry_skip_records_nothing_and_a_skip_has_no_elapsed(schema: str, tmp_path: Path) -> None:
    for version in (1, 2):
        write(tmp_path, f"{version}_t{version}.sql", f"CREATE TABLE t{version} (id int4);")
    migrator = Migrator(tmp_path)

    async def run() -> None:
        async with await connect(schema) as conn:
            planned = await migrator.skip(conn, dry_run=True)
            assert [r.migration.version for r in planned] == [1, 2]
            assert all(r.elapsed is None for r in planned)
            assert not await table_exists(conn, HISTORY)
            skipped = await migrator.skip(conn)
            assert [r.migration.version for r in skipped] == [1, 2]
            assert await history(conn) == [(1, True), (2, True)]

    asyncio.run(run())


def test_revert_results_carry_the_migration_and_info_reports_its_kind(schema: str, tmp_path: Path) -> None:
    write(tmp_path, "1_t1.sql", "CREATE TABLE t1 (id int4);")
    reversible(tmp_path, 2, "t2")
    migrator = Migrator(tmp_path)

    async def run() -> None:
        async with await connect(schema) as conn:
            applied = await migrator.run(conn)
            assert [r.migration for r in applied] == list(migrator.migrations)
            assert [s.kind for s in await migrator.info(conn)] == ["simple", "reversible"]
            (reverted,) = await migrator.revert(conn, target=1)
            assert reverted.migration.kind == "reversible"
            assert reverted.migration.down_path == tmp_path / "2_t2.down.sql"

    asyncio.run(run())


def test_a_failing_down_file_is_a_failed_error_with_the_down_path_and_the_reverts_before_it(
    schema: str, tmp_path: Path
) -> None:
    reversible(tmp_path, 1, "t1")
    write(tmp_path, "2_t2.up.sql", "CREATE TABLE t2 (id int4);")
    write(tmp_path, "2_t2.down.sql", "SELECT 1 / 0;")
    reversible(tmp_path, 3, "t3")
    migrator = Migrator(tmp_path)

    async def run() -> None:
        async with await connect(schema) as conn:
            await migrator.run(conn)
            with pytest.raises(MigrationFailedError) as exc:
                await migrator.revert(conn, all=True)
            failed = exc.value
            assert failed.version == 2
            assert failed.path == tmp_path / "2_t2.down.sql"
            assert failed.dirty is False
            assert failed.error.sqlstate == "22012"
            assert [r.migration.version for r in failed.applied] == [3]
            assert all(r.elapsed is not None for r in failed.applied)
            assert await history(conn) == [(1, True), (2, True)]

    asyncio.run(run())


def test_revert_of_a_version_missing_locally_is_a_missing_error_even_when_ignored(schema: str, tmp_path: Path) -> None:
    reversible(tmp_path, 1, "t1")
    reversible(tmp_path, 2, "t2")

    async def run() -> None:
        async with await connect(schema) as conn:
            await Migrator(tmp_path).run(conn)
            (tmp_path / "2_t2.up.sql").unlink()
            (tmp_path / "2_t2.down.sql").unlink()
            with pytest.raises(MigrationMissingError) as exc:
                await Migrator(tmp_path, ignore_missing=True).revert(conn)
            assert exc.value.versions == (2,)
            assert await history(conn) == [(1, True), (2, True)]

    asyncio.run(run())
