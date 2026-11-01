import asyncio
import os
import secrets
from typing import LiteralString

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from rowx.errors import DatabaseError, InterfaceError
from rowx.postgres import ConnectOptions, connect
from rowx.postgres._admin import create_database, database_exists, drop_database, list_databases

pytestmark = [pytest.mark.integration]


@pytest.fixture
def server_dsn() -> str:
    dsn = os.environ.get("ROWX_TEST_DSN")
    if dsn is not None:
        return dsn
    if os.environ.get("ROWX_REQUIRE_DB") == "1":
        pytest.fail("ROWX_REQUIRE_DB=1 but ROWX_TEST_DSN is not set")
    pytest.skip("ROWX_TEST_DSN is not set")


async def sql_in(dsn: str, database: str, statement: LiteralString) -> None:
    async with await psycopg.AsyncConnection.connect(
        make_conninfo(dsn, dbname=database),
        autocommit=True,
    ) as conn:
        await conn.execute(statement)


def unique(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


@pytest.mark.db
def test_create_exists_and_drop(server_dsn: str) -> None:
    name = unique("rowx_py_admin")

    async def run() -> None:
        assert not await database_exists(server_dsn, name)
        await create_database(server_dsn, name)
        assert await database_exists(server_dsn, name)
        with pytest.raises(DatabaseError) as exc:
            await create_database(server_dsn, name)
        assert exc.value.sqlstate == "42P04"
        await drop_database(server_dsn, name)
        assert not await database_exists(server_dsn, name)
        # a missing database is not an error
        await drop_database(server_dsn, name)

    asyncio.run(run())


@pytest.mark.db
def test_name_defaults_to_the_database_in_the_options(server_dsn: str) -> None:
    name = unique("rowx_py_default")
    options = ConnectOptions.from_dsn(server_dsn, database=name)

    async def run() -> None:
        assert not await database_exists(options)
        await create_database(options)
        try:
            assert await database_exists(options)
            async with await connect(options) as conn:
                await conn.ping()
        finally:
            await drop_database(options)
        assert not await database_exists(options)

    asyncio.run(run())


def test_unusable_names_are_interface_errors() -> None:
    async def run() -> None:
        with pytest.raises(InterfaceError):
            await database_exists("postgresql://u@127.0.0.1:1")
        # the server would truncate it, so a lookup by the full name would miss
        with pytest.raises(InterfaceError):
            await create_database("postgresql://u@127.0.0.1:1", "a" * 64)

    asyncio.run(run())


@pytest.mark.db
def test_template_and_quoted_names(server_dsn: str) -> None:
    template = f'a-b "{unique("tpl")}"'
    clone = unique("rowx_py_clone")

    async def run() -> None:
        await create_database(server_dsn, template)
        try:
            await sql_in(server_dsn, template, "CREATE TABLE marker (i int); INSERT INTO marker VALUES (7)")
            await create_database(server_dsn, clone, template=template)
            try:
                assert await database_exists(server_dsn, clone)
                await sql_in(server_dsn, clone, "DO $$ BEGIN ASSERT (SELECT i FROM marker) = 7; END $$")
            finally:
                await drop_database(server_dsn, clone)
        finally:
            await drop_database(server_dsn, template)
        assert not await database_exists(server_dsn, template)

    asyncio.run(run())


@pytest.mark.db
def test_force_drops_a_database_with_a_live_session(server_dsn: str) -> None:
    name = unique("rowx_py_force")

    async def run() -> None:
        await create_database(server_dsn, name)
        session = await connect(ConnectOptions.from_dsn(server_dsn, database=name))
        try:
            with pytest.raises(DatabaseError) as exc:
                await drop_database(server_dsn, name)
            assert exc.value.sqlstate == "55006"
            await drop_database(server_dsn, name, force=True)
            assert not await database_exists(server_dsn, name)
        finally:
            await session.close()

    asyncio.run(run())


@pytest.mark.db
def test_a_wrong_password_never_appears_in_an_error(server_dsn: str) -> None:
    needle = "hunter2-not-the-password"
    options = ConnectOptions.from_dsn(server_dsn, password=needle)

    async def run() -> None:
        with pytest.raises(DatabaseError) as exc:
            await database_exists(options, "anything")
        assert exc.value.sqlstate == "28P01"
        assert needle not in str(exc.value)
        assert needle not in repr(exc.value)

    asyncio.run(run())


@pytest.mark.db
def test_a_comment_round_trips_through_list_databases(server_dsn: str) -> None:
    prefix = unique("rowx_py_cmt")
    tagged, plain = f"{prefix}_a", f"{prefix}_b"
    comment = "rowx:1@it's:1700000000"

    async def run() -> None:
        await create_database(server_dsn, tagged, comment=comment)
        try:
            await create_database(server_dsn, plain)
            try:
                found = await list_databases(server_dsn, prefix)
                assert [(d.name, d.comment, d.sessions) for d in found] == [(tagged, comment, 0), (plain, None, 0)]
            finally:
                await drop_database(server_dsn, plain)
        finally:
            await drop_database(server_dsn, tagged)

    asyncio.run(run())
