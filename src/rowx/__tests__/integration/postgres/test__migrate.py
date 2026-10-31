import asyncio
from typing import TYPE_CHECKING, Final

import pytest

from rowx import query, query_unchecked
from rowx.errors import (
    ConnectionBusyError,
    DatabaseError,
    DirtyMigrationError,
    InterfaceError,
    MigrationChecksumError,
    MigrationFailedError,
    MigrationMissingError,
    MigrationOrderError,
    MigrationSourceError,
)
from rowx.postgres import Connection, Migrator, Pool, connect

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = [pytest.mark.integration, pytest.mark.db]

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
            assert [(s.version, s.description, s.state) for s in applied] == [
                (1, "first", "applied"),
                (2, "second", "applied"),
            ]
            assert all(s.installed_on is not None for s in applied)
            # the time is written after the commit, so it is a real duration
            assert all(s.execution_time is not None and s.execution_time.total_seconds() > 0 for s in applied)
            assert await table_exists(conn, "first")
            assert await table_exists(conn, "second")
            assert await history(conn) == [(1, True), (2, True)]
            assert await migrator.run(conn) == ()
            # the lock is released and the connection is usable
            assert await advisory_locks(conn, await backend_pid(conn)) == 0

    asyncio.run(run())


def test_run_on_a_pool_releases_the_connection_and_the_lock(schema: str, tmp_path: Path) -> None:
    write(tmp_path, "1_first.sql", "CREATE TABLE first (id int4);")
    migrator = Migrator(tmp_path)

    async def run() -> None:
        async with Pool(schema, max_size=1, acquire_timeout=10) as pool:
            pid = await backend_pid(pool)
            applied = await migrator.run(pool)
            assert [s.version for s in applied] == [1]
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
            assert [s.version for s in await migrator.run(conn, target=2)] == [1, 2]
            assert not await table_exists(conn, "t3")
            assert [s.version for s in await migrator.run(conn)] == [3]

    asyncio.run(run())


def test_undo_reverts_newest_first_to_a_target_and_to_zero(schema: str, tmp_path: Path) -> None:
    for version in (1, 2, 3):
        reversible(tmp_path, version, f"t{version}")
    migrator = Migrator(tmp_path)

    async def run() -> None:
        async with await connect(schema) as conn:
            await migrator.run(conn)
            reverted = await migrator.undo(conn, target=1)
            assert [(s.version, s.state) for s in reverted] == [(3, "pending"), (2, "pending")]
            assert await table_exists(conn, "t1")
            assert not await table_exists(conn, "t2")
            assert await history(conn) == [(1, True)]
            assert [s.version for s in await migrator.undo(conn, target=0)] == [1]
            assert not await table_exists(conn, "t1")
            assert await migrator.undo(conn, target=0) == ()
            # reverted migrations apply again
            assert [s.version for s in await migrator.run(conn)] == [1, 2, 3]

    asyncio.run(run())


def test_undo_without_a_down_file_raises_before_reverting_anything(schema: str, tmp_path: Path) -> None:
    reversible(tmp_path, 1, "t1")
    write(tmp_path, "2_t2.sql", "CREATE TABLE t2 (id int4);")
    migrator = Migrator(tmp_path)

    async def run() -> None:
        async with await connect(schema) as conn:
            await migrator.run(conn)
            with pytest.raises(MigrationSourceError) as exc:
                await migrator.undo(conn, target=0)
            assert exc.value.version == 2
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
            assert [(s.version, s.state) for s in skipped] == [(1, "applied"), (2, "applied")]
            assert all(s.installed_on is not None and s.execution_time is None for s in skipped)
            assert not await table_exists(conn, "t1")
            assert [s.version for s in await migrator.run(conn)] == [3]
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
            assert exc.value.version == 2
            assert [s.version for s in await Migrator(tmp_path, ignore_missing=True).run(conn)] == [3]

    asyncio.run(run())


def test_out_of_order_is_an_error_unless_allowed(schema: str, tmp_path: Path) -> None:
    write(tmp_path, "2_b.sql", "CREATE TABLE b (id int4);")

    async def run() -> None:
        async with await connect(schema) as conn:
            await Migrator(tmp_path).run(conn)
            write(tmp_path, "1_a.sql", "CREATE TABLE a (id int4);")
            with pytest.raises(MigrationOrderError) as exc:
                await Migrator(tmp_path).run(conn)
            assert exc.value.version == 1
            assert not await table_exists(conn, "a")
            assert [s.version for s in await Migrator(tmp_path, allow_out_of_order=True).run(conn)] == [1]

    asyncio.run(run())


def test_an_unknown_target_or_one_below_the_latest_is_an_order_error(schema: str, tmp_path: Path) -> None:
    write(tmp_path, "1_a.sql", "CREATE TABLE a (id int4);")
    write(tmp_path, "2_b.sql", "CREATE TABLE b (id int4);")
    migrator = Migrator(tmp_path)

    async def run() -> None:
        async with await connect(schema) as conn:
            with pytest.raises(MigrationOrderError) as unknown:
                await migrator.run(conn, target=99)
            assert unknown.value.version == 99
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
            for call in (migrator.run(tx), migrator.undo(tx, target=0), migrator.skip(tx), migrator.info(tx)):  # ty: ignore[invalid-argument-type]
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


def test_a_second_operation_on_a_busy_connection_is_a_busy_error(schema: str, tmp_path: Path) -> None:
    migrator = Migrator(tmp_path)

    async def run() -> None:
        async with await connect(schema) as conn:
            first = asyncio.ensure_future(migrator.info(conn))
            # lets the first call claim the connection
            await asyncio.sleep(0)
            with pytest.raises(ConnectionBusyError):
                await migrator.info(conn)
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


def test_two_migrators_on_one_database_both_finish(schema: str, tmp_path: Path) -> None:
    write(tmp_path, "1_t.sql", "CREATE TABLE t (id int4);")
    migrator = Migrator(tmp_path)

    async def run() -> list[int]:
        async with Pool(schema, max_size=2) as pool:
            results = await asyncio.gather(migrator.run(pool), migrator.run(pool))
            return sorted(len(r) for r in results)

    # one applied it, the other waited on the lock and found nothing to do
    assert asyncio.run(run()) == [0, 1]
