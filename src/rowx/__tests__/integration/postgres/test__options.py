import asyncio
from dataclasses import FrozenInstanceError
from typing import TYPE_CHECKING

import pytest

from rowx import query
from rowx.errors import InterfaceError
from rowx.postgres import ConnectOptions, connect

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from rowx.postgres import Connection

pytestmark = [pytest.mark.integration]

WORK_MEM = "3MB"


def run_with_conn[T](options: ConnectOptions, body: Callable[[Connection], Awaitable[T]]) -> T:
    async def run() -> T:
        async with await connect(options) as conn:
            return await body(conn)

    return asyncio.run(run())


def test_from_dsn_url_form() -> None:
    opts = ConnectOptions.from_dsn(
        "postgresql://app:s3cret@db.example:5433/appdb?sslmode=disable&application_name=svc&connect_timeout=7",
    )
    assert (opts.host, opts.port, opts.user, opts.password, opts.database) == (
        "db.example",
        5433,
        "app",
        "s3cret",
        "appdb",
    )
    assert (opts.sslmode, opts.application_name, opts.connect_timeout) == ("disable", "svc", 7.0)
    assert opts.statement_cache_size == 256
    assert not opts.server_settings
    assert not opts.extra


def test_from_dsn_key_value_form() -> None:
    opts = ConnectOptions.from_dsn(
        "host=db.example port=5433 user=app password='p w' dbname=appdb sslmode=verify-full sslrootcert=/ca.pem",
    )
    assert (opts.host, opts.port, opts.user, opts.password, opts.database) == (
        "db.example",
        5433,
        "app",
        "p w",
        "appdb",
    )
    assert (opts.sslmode, opts.sslrootcert) == ("verify-full", "/ca.pem")


def test_overrides_take_precedence_over_the_dsn() -> None:
    opts = ConnectOptions.from_dsn(
        "postgresql://app@h:5433/appdb?options=-c%20work_mem%3D1MB",
        port=6543,
        server_settings={"work_mem": "2MB"},
        statement_cache_size=0,
    )
    assert (opts.port, opts.statement_cache_size, dict(opts.server_settings)) == (6543, 0, {"work_mem": "2MB"})
    assert opts.host == "h"


def test_repr_hides_the_password() -> None:
    opts = ConnectOptions.from_dsn("postgresql://app:hunter2@h/db")
    assert opts.password == "hunter2"  # ruff: ignore[hardcoded-password-string]
    assert "hunter2" not in repr(opts)


def test_options_are_frozen() -> None:
    opts = ConnectOptions()
    with pytest.raises(FrozenInstanceError):
        opts.host = "x"  # type: ignore[misc]  # ty: ignore[invalid-assignment]


def test_extra_keys_pass_through() -> None:
    opts = ConnectOptions.from_dsn("host=h keepalives=0 target_session_attrs=any")
    assert dict(opts.extra) == {"keepalives": "0", "target_session_attrs": "any"}


def test_multiple_hosts_are_kept_in_extra() -> None:
    opts = ConnectOptions.from_dsn("postgresql://u@a:1,b:2/db")
    assert opts.host is None
    assert dict(opts.extra) == {"host": "a,b", "port": "1,2"}


@pytest.mark.parametrize("dsn", ["host=h sslcert=/x", "postgresql://h/db?sslkey=/x", "host=h sslcrl=/x"])
def test_unsupported_ssl_keys_are_interface_errors(dsn: str) -> None:
    with pytest.raises(InterfaceError):
        ConnectOptions.from_dsn(dsn)


@pytest.mark.db
def test_schema_dsn_round_trips_its_search_path(schema: str) -> None:
    opts = ConnectOptions.from_dsn(schema)
    name = schema.split("search_path=")[1].strip("'")
    assert dict(opts.server_settings) == {"search_path": name}

    async def body(conn: Connection) -> str:
        return await query[str](t"SELECT current_schema()").one(conn)

    assert run_with_conn(opts, body) == name


@pytest.mark.db
def test_server_settings_show_up_and_survive_reset(schema: str) -> None:
    base = ConnectOptions.from_dsn(schema)
    opts = ConnectOptions.from_dsn(schema, server_settings={**base.server_settings, "work_mem": WORK_MEM})

    async def body(conn: Connection) -> tuple[str, str, str]:
        before = await query[str](t"SHOW work_mem").one(conn)
        await query(t"SET work_mem = '9MB'").execute(conn)
        await conn.reset()
        after = await query[str](t"SHOW work_mem").one(conn)
        schema_after = await query[str](t"SHOW search_path").one(conn)
        return before, after, schema_after

    before, after, schema_after = run_with_conn(opts, body)
    assert (before, after) == (WORK_MEM, WORK_MEM)
    assert schema_after == dict(base.server_settings)["search_path"]


@pytest.mark.db
def test_application_name_reaches_the_server(schema: str) -> None:
    opts = ConnectOptions.from_dsn(schema, application_name="rowx-opts-test")

    async def body(conn: Connection) -> str:
        return await query[str](t"SHOW application_name").one(conn)

    assert run_with_conn(opts, body) == "rowx-opts-test"
