import asyncio
from dataclasses import dataclass
from string.templatelib import Interpolation, Template
from typing import TYPE_CHECKING, NamedTuple, TypedDict

import pytest
from rowx_core.errors import (
    ConnectionBusyError,
    DatabaseError,
    DecodeError,
    EncodeError,
    InterfaceError,
    RowNotFound,
    RowShapeError,
)
from rowx_sqlite import Connection, ConnectOptions, connect
from rowx_sqlite._compiler import MAX_PARAMETERS

from rowx import query, query_unchecked

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class User:
    id: int
    name: str


class UserRow(NamedTuple):
    id: int
    name: str


class UserDict(TypedDict):
    id: int
    name: str


# the SQL or the parameter count is built at run time, so these go to the native connection that the query objects call;
# the positional flags are its fixed argument list
async def native_fetch(conn: Connection, sql: str, params: list[object]) -> list[object]:
    return await conn._impl._fetch(sql, params, None, False, True, None, None)  # ruff: ignore[boolean-positional-value-in-call]


def native_stream(conn: Connection, sql: str, params: list[object]) -> object:
    return conn._impl._stream(sql, params, None, False, None, 10, None)  # ruff: ignore[boolean-positional-value-in-call]


async def seed(conn: Connection) -> None:
    await query(t"CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL)").execute(conn)
    for ident, name in [(1, "ada"), (2, "grace"), (3, "edsger")]:
        await query(t"INSERT INTO users (id, name) VALUES ({ident}, {name})").execute(conn)


@pytest.mark.asyncio
async def test_fetch_all_returns_tuples(conn: Connection) -> None:
    await seed(conn)
    rows = await query[tuple[int, str]](t"SELECT id, name FROM users ORDER BY id").all(conn)
    assert rows == [(1, "ada"), (2, "grace"), (3, "edsger")]


@pytest.mark.asyncio
async def test_fetch_binds_parameters(conn: Connection) -> None:
    await seed(conn)
    wanted = 2
    assert await query[tuple[int, str]](t"SELECT id, name FROM users WHERE id = {wanted}").one(conn) == (2, "grace")


@pytest.mark.asyncio
async def test_scalar_rows(conn: Connection) -> None:
    await seed(conn)
    assert await query[int](t"SELECT count(*) FROM users").one(conn) == 3
    assert await query[str](t"SELECT name FROM users ORDER BY id").all(conn) == ["ada", "grace", "edsger"]


@pytest.mark.asyncio
async def test_one_optional_and_missing_rows(conn: Connection) -> None:
    await seed(conn)
    missing = 99
    statement = query[str](t"SELECT name FROM users WHERE id = {missing}")
    assert await statement.optional(conn) is None
    with pytest.raises(RowNotFound):
        await statement.one(conn)


@pytest.mark.asyncio
async def test_named_rows(conn: Connection) -> None:
    await seed(conn)
    users = await query[User](t"SELECT id, name FROM users ORDER BY id").all(conn)
    assert users == [User(1, "ada"), User(2, "grace"), User(3, "edsger")]
    assert await query[UserRow](t"SELECT id, name FROM users WHERE id = 1").one(conn) == UserRow(1, "ada")
    assert await query[UserDict](t"SELECT name, id FROM users WHERE id = 2").one(conn) == {"id": 2, "name": "grace"}


@pytest.mark.asyncio
async def test_a_named_row_that_misses_a_field_is_a_shape_error(conn: Connection) -> None:
    await seed(conn)
    with pytest.raises(RowShapeError):
        await query[User](t"SELECT id FROM users").all(conn)


@pytest.mark.asyncio
async def test_a_null_in_a_non_nullable_column_is_a_shape_error(conn: Connection) -> None:
    await query(t"CREATE TABLE t (a INTEGER)").execute(conn)
    await query(t"INSERT INTO t VALUES (NULL)").execute(conn)
    with pytest.raises(RowShapeError):
        await query[int](t"SELECT a FROM t").all(conn)
    assert await query[int | None](t"SELECT a FROM t").all(conn) == [None]


@pytest.mark.asyncio
async def test_a_stored_value_that_disagrees_with_its_column_is_a_decode_error(conn: Connection) -> None:
    await query(t"CREATE TABLE t (a INTEGER)").execute(conn)
    await query(t"INSERT INTO t VALUES ('not a number')").execute(conn)
    with pytest.raises(DecodeError):
        await query[int](t"SELECT a FROM t").all(conn)


@pytest.mark.asyncio
async def test_streams_deliver_every_row_in_order(conn: Connection) -> None:
    await seed(conn)
    stream = query[int](t"SELECT id FROM users ORDER BY id").fetch(conn, batch_size=2)
    assert [row async for row in stream] == [1, 2, 3]


@pytest.mark.asyncio
async def test_a_stream_of_a_write_reports_the_rows_it_changed(conn: Connection) -> None:
    await seed(conn)
    stream = query[int](t"UPDATE users SET name = upper(name) RETURNING id").fetch(conn)
    assert sorted([row async for row in stream]) == [1, 2, 3]
    assert stream.result is not None
    assert stream.result.rows_affected == 3


@pytest.mark.asyncio
async def test_closing_a_stream_early_frees_the_connection(conn: Connection) -> None:
    await query(
        t"CREATE TABLE n AS WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 5000) SELECT x FROM c",
    ).execute(conn)
    stream = query[int](t"SELECT x FROM n").fetch(conn, batch_size=10)
    assert await anext(stream) == 1
    await stream.aclose()
    assert await query[int](t"SELECT count(*) FROM n").one(conn) == 5000


@pytest.mark.asyncio
async def test_a_stream_holds_the_connection_until_it_ends(conn: Connection) -> None:
    await seed(conn)
    stream = query[int](t"SELECT id FROM users").fetch(conn, batch_size=1)
    assert await anext(stream) in {1, 2, 3}
    with pytest.raises(ConnectionBusyError):
        await query[int](t"SELECT 1").one(conn)
    await stream.aclose()
    assert await query[int](t"SELECT 1").one(conn) == 1


@pytest.mark.asyncio
async def test_a_stream_that_fits_in_the_buffer_still_holds_the_connection_until_it_is_read_out(
    conn: Connection,
) -> None:
    await seed(conn)
    stream = query[int](t"SELECT id FROM users ORDER BY id").fetch(conn, batch_size=1)
    assert await anext(stream) == 1
    with pytest.raises(ConnectionBusyError):
        await query[int](t"SELECT 1").one(conn)
    assert [row async for row in stream] == [2, 3]
    assert await query[int](t"SELECT 1").one(conn) == 1


@pytest.mark.asyncio
async def test_a_transaction_cannot_commit_while_its_stream_is_open(conn: Connection) -> None:
    await seed(conn)
    async with conn.transaction() as tx:
        stream = query[int](t"SELECT id FROM users ORDER BY id").fetch(tx, batch_size=1)
        assert await anext(stream) == 1
        with pytest.raises(ConnectionBusyError):
            await tx.commit()
        assert [row async for row in stream] == [2, 3]


@pytest.mark.asyncio
async def test_execute_reports_rows_affected(conn: Connection) -> None:
    await seed(conn)
    assert (await query(t"UPDATE users SET name = 'x' WHERE id < 3").execute(conn)).rows_affected == 2
    assert (await query(t"CREATE TABLE other (a)").execute(conn)).rows_affected == 0


@pytest.mark.asyncio
async def test_execute_refuses_a_statement_that_returns_rows(conn: Connection) -> None:
    await seed(conn)
    with pytest.raises(InterfaceError, match="returns rows"):
        await query(t"SELECT * FROM users").execute(conn)
    # the refusal came before anything ran
    assert await query[int](t"SELECT count(*) FROM users").one(conn) == 3


@pytest.mark.asyncio
async def test_a_second_call_before_the_first_settles_is_busy(conn: Connection) -> None:
    statement = query[int](t"SELECT 1")
    results = await asyncio.gather(statement.one(conn), statement.one(conn), return_exceptions=True)
    assert results[0] == 1
    assert isinstance(results[1], ConnectionBusyError)


@pytest.mark.asyncio
async def test_the_compile_ceiling_is_the_connections_default_limit(conn: Connection) -> None:
    # the compiler's ceiling is the bundled build's maximum, which a connection starts at
    assert conn._impl.variable_limit == MAX_PARAMETERS
    one = 1
    items = Template(*[part for i in range(MAX_PARAMETERS) for part in (", " if i else "", Interpolation(one, "one"))])
    assert await query_unchecked[int](t"SELECT 1 WHERE 1 IN ({items})").one(conn) == 1


@pytest.mark.asyncio
async def test_the_parameter_count_is_checked_against_the_connections_limit(conn: Connection) -> None:
    impl = conn._impl
    limit = impl.variable_limit
    assert limit > 0
    # a statement at the limit runs
    assert await native_fetch(conn, f"SELECT ?{limit}", [None] * limit) == [(None,)]
    with pytest.raises(InterfaceError, match="SQLITE_LIMIT_VARIABLE_NUMBER"):
        await native_fetch(conn, "SELECT ?1", [None] * (limit + 1))
    with pytest.raises(InterfaceError, match="SQLITE_LIMIT_VARIABLE_NUMBER"):
        await impl._execute("SELECT ?1", [None] * (limit + 1))
    with pytest.raises(InterfaceError, match="SQLITE_LIMIT_VARIABLE_NUMBER"):
        native_stream(conn, "SELECT ?1", [None] * (limit + 1))
    # nothing was left claimed
    assert await query[int](t"SELECT 1").one(conn) == 1


@pytest.mark.asyncio
async def test_cancelling_a_running_query_leaves_the_connection_usable(conn: Connection) -> None:
    long_query = query[int](
        t"WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c WHERE n < 1000000000000000) SELECT count(*) FROM c",
    )
    task = asyncio.ensure_future(long_query.one(conn))
    # whether the statement is stepping yet or not, the cancel stops it
    for _ in range(5):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for n in range(1, 4):
        assert await query[int](t"SELECT {n}").one(conn) == n


@pytest.mark.asyncio
async def test_cancelling_a_stream_ends_it(conn: Connection) -> None:
    endless = query[int](
        t"WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c WHERE n < 1000000000000000) SELECT n FROM c",
    )
    stream = endless.fetch(conn, batch_size=10)
    assert await anext(stream) == 1
    await stream.aclose()
    assert await query[int](t"SELECT 5").one(conn) == 5


@pytest.mark.asyncio
async def test_closing_the_connection_during_a_running_fetch_is_an_interface_error(conn: Connection) -> None:
    long_query = query[int](
        t"WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c WHERE n < 1000000000000000) SELECT count(*) FROM c",
    )
    task = asyncio.ensure_future(long_query.one(conn))
    for _ in range(5):
        await asyncio.sleep(0)
    await conn.close()
    with pytest.raises(InterfaceError):
        await task


@pytest.mark.asyncio
async def test_closing_the_connection_during_a_running_execute_is_an_interface_error(conn: Connection) -> None:
    await seed(conn)
    long_write = query(
        t"INSERT INTO users (id, name) WITH RECURSIVE c(n) AS (SELECT 10 UNION ALL SELECT n + 1 FROM c WHERE n < 1000000000000000) SELECT n, 'x' FROM c",
    )
    task = asyncio.ensure_future(long_write.execute(conn))
    for _ in range(5):
        await asyncio.sleep(0)
    await conn.close()
    with pytest.raises(InterfaceError):
        await task


@pytest.mark.asyncio
async def test_the_statement_timeout_interrupts_a_long_statement(tmp_path: Path) -> None:
    options = ConnectOptions(path=str(tmp_path / "t.db"), mode="rwc", statement_timeout=0.2)
    conn = await connect(options)
    try:
        long_query = query[int](
            t"WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c WHERE n < 1000000000000000) SELECT count(*) FROM c",
        )
        with pytest.raises(DatabaseError) as info:
            await long_query.one(conn)
        assert info.value.sqlstate == "57014"
        assert info.value.code == "SQLITE_INTERRUPT"
        assert await query[int](t"SELECT 1").one(conn) == 1
    finally:
        await conn.close()


SCHEMA = (
    "CREATE TABLE u (id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, age INTEGER CHECK (age >= 0), parent INTEGER REFERENCES u (id))",
    "CREATE TABLE s (n INTEGER) STRICT",
    "CREATE TABLE g (n INTEGER)",
    "CREATE TRIGGER g_no BEFORE INSERT ON g BEGIN SELECT RAISE(ABORT, 'no'); END",
    "INSERT INTO u (id, email, age) VALUES (2, 'a@x', 1)",
)


# the SQL comes from the parameters, so it goes to the native connection, which is what the query objects call
async def run_raw(conn: Connection, sql: str) -> None:
    impl = conn._impl
    if sql.startswith("SELECT"):
        await native_fetch(conn, sql, [])
    else:
        await impl._execute(sql, [])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sql", "sqlstate", "code"),
    [
        ("INSERT INTO u (id, email, age, parent) VALUES (2, 'x', 1, NULL)", "23505", "SQLITE_CONSTRAINT_PRIMARYKEY"),
        ("INSERT INTO u (id, email, age, parent) VALUES (3, 'a@x', 1, NULL)", "23505", "SQLITE_CONSTRAINT_UNIQUE"),
        ("INSERT INTO u (id, email, age, parent) VALUES (3, NULL, 1, NULL)", "23502", "SQLITE_CONSTRAINT_NOTNULL"),
        ("INSERT INTO u (id, email, age, parent) VALUES (3, 'b@x', 1, 99)", "23503", "SQLITE_CONSTRAINT_FOREIGNKEY"),
        ("INSERT INTO u (id, email, age, parent) VALUES (3, 'b@x', -1, NULL)", "23514", "SQLITE_CONSTRAINT_CHECK"),
        ("INSERT INTO s (n) VALUES ('text')", "22000", "SQLITE_CONSTRAINT_DATATYPE"),
        ("INSERT INTO g (n) VALUES (1)", "23000", "SQLITE_CONSTRAINT_TRIGGER"),
        ("INSERT INTO u (id, email) VALUES ('x', 'z')", "22000", "SQLITE_MISMATCH"),
        ("SELECT zeroblob(2000000000)", "54000", "SQLITE_TOOBIG"),
        ("SELEC 1", "HY000", "SQLITE_ERROR"),
        ("SELECT * FROM missing", "HY000", "SQLITE_ERROR"),
    ],
)
async def test_each_error_maps_to_its_sqlstate_and_code(conn: Connection, sql: str, sqlstate: str, code: str) -> None:
    for ddl in SCHEMA:
        await run_raw(conn, ddl)
    with pytest.raises(DatabaseError) as info:
        await run_raw(conn, sql)
    assert info.value.sqlstate == sqlstate
    assert info.value.code == code


async def fill(conn: Connection) -> None:
    for _ in range(100):
        await run_raw(conn, "INSERT INTO f VALUES (zeroblob(100000))")


@pytest.mark.asyncio
async def test_a_full_database_maps_to_53100(conn: Connection) -> None:
    await run_raw(conn, "CREATE TABLE f (b BLOB)")
    await native_fetch(conn, "PRAGMA max_page_count = 10", [])
    with pytest.raises(DatabaseError) as info:
        await fill(conn)
    assert info.value.sqlstate == "53100"
    assert info.value.code == "SQLITE_FULL"


@pytest.mark.asyncio
async def test_an_error_reports_a_one_based_character_position(conn: Connection) -> None:
    sql = "SELECT 'é', nope"
    with pytest.raises(DatabaseError) as info:
        await run_raw(conn, sql)
    assert info.value.sqlstate == "HY000"
    # SQLite counts the offset of `nope` in bytes (the two-byte `é` counts twice); the position counts characters
    assert info.value.position == sql.index("nope") + 1


@pytest.mark.asyncio
async def test_an_error_carries_the_sqlite_message(conn: Connection) -> None:
    with pytest.raises(DatabaseError) as info:
        await run_raw(conn, "SELECT * FROM missing")
    assert "no such table" in info.value.message


@pytest.mark.asyncio
async def test_a_locked_database_is_busy(tmp_path: Path) -> None:
    path = str(tmp_path / "busy.db")
    holder = await connect(ConnectOptions(path=path, mode="rwc"))
    waiter = await connect(ConnectOptions(path=path, connect_timeout=0.05))
    try:
        await run_raw(holder, "CREATE TABLE t (a)")
        async with holder.transaction():
            with pytest.raises(DatabaseError) as info:
                await run_raw(waiter, "INSERT INTO t VALUES (1)")
            assert info.value.sqlstate == "55P03"
            assert info.value.code == "SQLITE_BUSY"
    finally:
        await holder.close()
        await waiter.close()


@pytest.mark.asyncio
async def test_a_read_only_connection_refuses_writes(tmp_path: Path) -> None:
    path = str(tmp_path / "ro.db")
    writer = await connect(ConnectOptions(path=path, mode="rwc"))
    await run_raw(writer, "CREATE TABLE t (a)")
    await writer.close()
    conn = await connect(ConnectOptions(path=path, read_only=True))
    try:
        assert await query[int](t"SELECT count(*) FROM t").one(conn) == 0
        with pytest.raises(DatabaseError) as info:
            await run_raw(conn, "INSERT INTO t VALUES (1)")
        assert info.value.sqlstate == "25006"
        assert info.value.code == "SQLITE_READONLY"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_a_missing_file_is_not_created_unless_asked(tmp_path: Path) -> None:
    path = tmp_path / "missing.db"
    with pytest.raises(DatabaseError) as info:
        await connect(ConnectOptions(path=str(path)))
    assert info.value.sqlstate == "58030"
    assert info.value.code == "SQLITE_CANTOPEN"
    assert not path.exists()
    created = await connect(ConnectOptions(path=str(path), mode="rwc"))
    await created.close()
    assert path.exists()


@pytest.mark.asyncio
async def test_a_file_that_is_not_a_database_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "text.db"
    path.write_bytes(b"this is not a database file" * 100)
    with pytest.raises(DatabaseError) as info:
        await connect(ConnectOptions(path=str(path)))
    assert info.value.sqlstate == "XX002"
    assert info.value.code == "SQLITE_NOTADB"


@pytest.mark.asyncio
async def test_an_interrupted_statement_maps_to_57014_with_its_code(tmp_path: Path) -> None:
    conn = await connect(ConnectOptions(path=str(tmp_path / "i.db"), mode="rwc", statement_timeout=0.1))
    try:
        with pytest.raises(DatabaseError) as info:
            await run_raw(
                conn,
                "SELECT count(*) FROM (WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c WHERE n < 1000000000000000) SELECT n FROM c)",
            )
        assert (info.value.sqlstate, info.value.code) == ("57014", "SQLITE_INTERRUPT")
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_connect_timeout_sets_the_busy_timeout() -> None:
    default = await connect(ConnectOptions(memory=True))
    custom = await connect(ConnectOptions(memory=True, connect_timeout=1.5))
    try:
        assert await native_fetch(default, "PRAGMA busy_timeout", []) == [(5000,)]
        assert await native_fetch(custom, "PRAGMA busy_timeout", []) == [(1500,)]
    finally:
        await default.close()
        await custom.close()


@pytest.mark.asyncio
async def test_a_statement_timeout_of_zero_means_unlimited() -> None:
    conn = await connect(ConnectOptions(memory=True, statement_timeout=0))
    try:
        assert await query[int](t"SELECT 1").one(conn) == 1
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_connect_accepts_a_dsn_and_memory(tmp_path: Path) -> None:
    memory = await connect("sqlite::memory:")
    try:
        assert await query[int](t"SELECT 1").one(memory) == 1
    finally:
        await memory.close()
    path = tmp_path / "dsn.db"
    path.touch()
    file = await connect(f"sqlite:///{path}")
    await file.close()


@pytest.mark.asyncio
async def test_connect_rejects_a_bad_dsn() -> None:
    with pytest.raises(InterfaceError, match="sqlite:///"):
        await connect("postgresql://localhost/db")


@pytest.mark.asyncio
async def test_connect_rejects_contradictory_options() -> None:
    with pytest.raises(InterfaceError, match="mutually exclusive"):
        await connect(ConnectOptions(path="x.db", memory=True))
    with pytest.raises(InterfaceError, match="required"):
        await connect(ConnectOptions())
    with pytest.raises(InterfaceError, match="mode"):
        await connect(ConnectOptions(memory=True, mode="bogus"))  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="statement_timeout"):
        await connect(ConnectOptions(memory=True, statement_timeout=-1))


@pytest.mark.asyncio
async def test_close_is_idempotent_and_ends_use(conn: Connection) -> None:
    assert not conn.closed
    await conn.close()
    assert conn.closed
    await conn.close()
    with pytest.raises(InterfaceError):
        await query[int](t"SELECT 1").one(conn)


@pytest.mark.asyncio
async def test_ping(conn: Connection) -> None:
    await conn.ping()


@pytest.mark.asyncio
async def test_a_bad_parameter_is_an_encode_error_and_runs_nothing(conn: Connection) -> None:
    await query(t"CREATE TABLE t (a)").execute(conn)
    bad = object()
    with pytest.raises(EncodeError, match="cannot be bound"):
        await query(t"INSERT INTO t VALUES ({bad})").execute(conn)
    assert await query[int](t"SELECT count(*) FROM t").one(conn) == 0


@pytest.mark.asyncio
async def test_a_statement_that_names_more_parameters_than_given_is_an_interface_error(conn: Connection) -> None:
    with pytest.raises(InterfaceError):
        await native_fetch(conn, "SELECT ?1, ?2", [1])
    assert await query[int](t"SELECT 1").one(conn) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("sql", ["", "   ", "-- c", "/* c */", ";"])
async def test_sql_without_a_statement_is_an_interface_error(conn: Connection, sql: str) -> None:
    with pytest.raises(InterfaceError, match="the SQL holds no statement"):
        await conn._impl._execute(sql, [])
    with pytest.raises(InterfaceError, match="the SQL holds no statement"):
        await native_fetch(conn, sql, [])
    stream = conn._impl._stream(sql, [], None, False, None, 10, None)  # ruff: ignore[boolean-positional-value-in-call]
    with pytest.raises(InterfaceError, match="the SQL holds no statement"):
        await stream._next()
    # through the query objects, the missing statement is reported before the row type is checked
    with pytest.raises(InterfaceError, match="the SQL holds no statement"):
        await query(t"").execute(conn)
    with pytest.raises(InterfaceError, match="the SQL holds no statement"):
        await query[int](t"-- c").all(conn)
    # the connection is still usable
    assert await query[int](t"SELECT 1").one(conn) == 1


@pytest.mark.asyncio
async def test_several_statements_are_refused_at_run_time_before_any_runs(conn: Connection) -> None:
    await seed(conn)
    with pytest.raises(InterfaceError):
        await query[int](t"SELECT 1; SELECT 2").all(conn)
    with pytest.raises(InterfaceError):
        await query[int](t"SELECT 1; SELECT 2").one(conn)
    with pytest.raises(InterfaceError):
        await query(t"DELETE FROM users WHERE id = 1; DELETE FROM users").execute(conn)
    with pytest.raises(InterfaceError):
        _ = [row async for row in query[int](t"SELECT 1; SELECT 2").fetch(conn, batch_size=2)]
    assert await query[int](t"SELECT count(*) FROM users").one(conn) == 3
