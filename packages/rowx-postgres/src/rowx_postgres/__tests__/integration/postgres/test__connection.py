import asyncio
import contextlib
import inspect
import os
import random
import re
import secrets
import time
from pathlib import Path
from typing import TYPE_CHECKING, Final
from urllib.parse import urlparse

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from rowx_core.errors import ConnectionBusyError, DatabaseError, InterfaceError
from rowx_postgres import RESET_QUERY, Connection, ConnectOptions, connect
from rowx_postgres._core import _cancel_requests_sent

import rowx
from rowx import query, query_unchecked

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator
    from string.templatelib import Template

    from rowx_postgres._core import Connection as ConnectionImpl

pytestmark = [pytest.mark.db]

POLL_LIMIT: Final[int] = 2000
ADVISORY_KEY: Final[int] = 727_001


def run_with_two[T](dsn: str, body: Callable[[Connection, Connection], Awaitable[T]]) -> T:
    # the second connection observes the first through the catalog
    async def run() -> T:
        async with await connect(dsn) as conn, await connect(dsn) as observer:
            return await body(conn, observer)

    return asyncio.run(run())


async def backend_pid(conn: Connection) -> int:
    return await query[int](t"SELECT pg_backend_pid()").one(conn)


async def activity(observer: Connection, pid: int) -> tuple[str, str]:
    return await query[tuple[str, str]](t"SELECT state, query FROM pg_stat_activity WHERE pid = {pid}").one(observer)


def test_ping_works(schema: str) -> None:
    async def body(conn: Connection, _observer: Connection) -> None:
        await conn.ping()
        await conn.ping()
        assert await query[int](t"SELECT 1").one(conn) == 1

    run_with_two(schema, body)


def test_ping_on_a_closed_connection_is_an_interface_error(schema: str) -> None:
    async def run() -> None:
        conn = await connect(schema)
        await conn.close()
        await conn.ping()

    with pytest.raises(InterfaceError):
        asyncio.run(run())


@pytest.mark.timing
def test_ping_on_a_busy_connection_raises_busy(schema: str) -> None:
    async def body(conn: Connection, observer: Connection) -> None:
        pid = await backend_pid(conn)
        slow = asyncio.create_task(query(t"SELECT pg_sleep(1)").execute(conn))
        for _ in range(POLL_LIMIT):
            state, _ = await activity(observer, pid)
            if state == "active":
                break
        else:
            pytest.fail("the sleeping query never became active")
        with pytest.raises(ConnectionBusyError):
            await conn.ping()
        await slow
        await conn.ping()

    run_with_two(schema, body)


def test_reset_rolls_back_an_open_transaction_from_a_raw_begin(schema: str) -> None:
    async def body(conn: Connection, observer: Connection) -> tuple[str, str, bool]:
        pid = await backend_pid(conn)
        await query(t"BEGIN").execute(conn)
        await query(t"CREATE TEMP TABLE rolled_back (id int)").execute(conn)
        during, _ = await activity(observer, pid)
        await conn.reset()
        after, last = await activity(observer, pid)
        exists = await query[bool](t"SELECT to_regclass('pg_temp.rolled_back') IS NOT NULL").one(conn)
        assert last.startswith("ROLLBACK;")
        return during, after, exists

    assert run_with_two(schema, body) == ("idle in transaction", "idle", False)


def test_reset_rolls_back_a_failed_transaction(schema: str) -> None:
    async def body(conn: Connection, observer: Connection) -> tuple[str, str, int]:
        pid = await backend_pid(conn)
        await query(t"BEGIN").execute(conn)
        with pytest.raises(DatabaseError):
            await query[int](t"SELECT 1 / 0").one(conn)
        during, _ = await activity(observer, pid)
        await conn.reset()
        after, _ = await activity(observer, pid)
        return during, after, await query[int](t"SELECT 1").one(conn)

    assert run_with_two(schema, body) == ("idle in transaction (aborted)", "idle", 1)


def test_reset_rolls_back_a_savepoint_started_transaction(schema: str) -> None:
    async def body(conn: Connection, observer: Connection) -> str:
        pid = await backend_pid(conn)
        await query(t"START TRANSACTION").execute(conn)
        await conn.reset()
        state, _ = await activity(observer, pid)
        return state

    assert run_with_two(schema, body) == "idle"


def test_reset_sends_no_rollback_when_nothing_is_open(schema: str) -> None:
    # a ROLLBACK outside a transaction makes the server log WARNING 25P01, so the statement text
    # the server saw last is the proof that none was sent
    async def body(conn: Connection, observer: Connection) -> tuple[str, str]:
        pid = await backend_pid(conn)
        await conn.reset()
        await conn.reset()
        state, last = await activity(observer, pid)
        return state, last

    state, last = run_with_two(schema, body)
    assert state == "idle"
    # the idle guard goes in front of the reset query when no transaction can be open
    assert last.startswith("SELECT 1 OPERATOR(pg_catalog./)")
    assert last.endswith(RESET_QUERY)
    assert "ROLLBACK" not in last


def test_reset_clears_the_flag_after_a_rollback(schema: str) -> None:
    async def body(conn: Connection, observer: Connection) -> str:
        pid = await backend_pid(conn)
        await query(t"BEGIN").execute(conn)
        await conn.reset()
        await conn.reset()
        _, last = await activity(observer, pid)
        return last

    assert "ROLLBACK" not in run_with_two(schema, body)


def test_reset_releases_advisory_locks(schema: str) -> None:
    async def body(conn: Connection, observer: Connection) -> tuple[int, int]:
        pid = await backend_pid(conn)
        count = query[int](t"SELECT count(*)::int4 FROM pg_locks WHERE locktype = 'advisory' AND pid = {pid}")
        await query(t"SELECT pg_advisory_lock({ADVISORY_KEY})").execute(conn)
        held = await count.one(observer)
        await conn.reset()
        return held, await count.one(observer)

    assert run_with_two(schema, body) == (1, 0)


def test_reset_clears_listen_registrations_and_sets(schema: str) -> None:
    async def body(conn: Connection, _observer: Connection) -> tuple[int, int, str, str]:
        listening = query[int](t"SELECT count(*)::int4 FROM pg_listening_channels()")
        original = await query[str](t"SHOW work_mem").one(conn)
        await query(t"LISTEN rowx_reset_test").execute(conn)
        await query(t"SET work_mem = '11MB'").execute(conn)
        listened, changed = await listening.one(conn), await query[str](t"SHOW work_mem").one(conn)
        await conn.reset()
        assert changed == "11MB"
        return listened, await listening.one(conn), original, await query[str](t"SHOW work_mem").one(conn)

    listened, after, original, restored = run_with_two(schema, body)
    assert (listened, after) == (1, 0)
    assert restored == original


def test_reset_leaves_prepared_statements_unchanged(schema: str) -> None:
    async def body(conn: Connection, _observer: Connection) -> tuple[int, int]:
        count = query[int](t"SELECT count(*)::int4 FROM pg_prepared_statements")
        await query[int](t"SELECT 1").one(conn)
        before = await count.one(conn)
        await conn.reset()
        return before, await count.one(conn)

    before, after = run_with_two(schema, body)
    assert before > 0
    assert before == after


DRIFT_ATTEMPTS: Final[int] = 1000
DRIFTING_ROWS: Final[Template] = t"""
    SELECT set_config('client_encoding', 'LATIN1', false), convert_from(decode('c383c2a9', 'hex'), 'UTF8')
"""


async def first_failure(conn: Connection, match: str) -> None:
    # the report is processed right after the statement completes, so re-issue a call instead of sleeping
    failure: InterfaceError | None = None
    for _ in range(DRIFT_ATTEMPTS):
        try:
            await query[int](t"SELECT 1").one(conn)
        except InterfaceError as err:
            failure = err
            break
        await asyncio.sleep(0)
    assert failure is not None
    assert re.search(match, str(failure))


async def drift(conn: Connection, statement: Template, match: str) -> None:
    # the statement itself fails once its own report is seen, so its error is allowed here
    with contextlib.suppress(InterfaceError):
        await query(statement).execute(conn)
    await first_failure(conn, match)


async def assert_drifted(conn: Connection, match: str) -> None:
    # every later call fails the same way, whatever the session sets back
    for _ in range(2):
        with pytest.raises(InterfaceError, match=match):
            await query[int](t"SELECT 1").one(conn)
    with pytest.raises(InterfaceError, match=match):
        await conn.ping()


def test_a_changed_client_encoding_fails_the_next_call_naming_it(schema: str) -> None:
    async def run() -> None:
        async with await connect(schema) as conn:
            assert await query[int](t"SELECT 1").one(conn) == 1
            await drift(conn, t"SET client_encoding = 'SJIS'", r"changed client_encoding to SJIS; rowx requires UTF8")
            await assert_drifted(conn, r"changed client_encoding to SJIS; rowx requires UTF8")

    asyncio.run(run())


def test_the_statement_that_changes_the_encoding_itself_fails(schema: str) -> None:
    async def run() -> None:
        async with await connect(schema) as conn:
            with pytest.raises(InterfaceError, match="changed client_encoding to SJIS"):
                await query(t"SET client_encoding = 'SJIS'").execute(conn)

    asyncio.run(run())


def test_standard_conforming_strings_off_fails_the_next_call(schema: str) -> None:
    async def run() -> None:
        async with await connect(schema) as conn:
            await drift(
                conn, t"SET standard_conforming_strings = off", r"standard_conforming_strings off; rowx requires on"
            )
            await assert_drifted(conn, r"standard_conforming_strings off; rowx requires on")

    asyncio.run(run())


def test_setting_the_same_client_encoding_keeps_the_connection(schema: str) -> None:
    async def run() -> int:
        async with await connect(schema) as conn:
            await query(t"SET client_encoding = 'UTF8'").execute(conn)
            await query(t"SET client_encoding = 'utf8'").execute(conn)
            await query(t"SET standard_conforming_strings = on").execute(conn)
            return await query[int](t"SELECT 1").one(conn)

    assert asyncio.run(run()) == 1


@pytest.mark.parametrize("alias", ["UNICODE", "unicode"])
def test_the_unicode_alias_keeps_the_connection(schema: str, alias: str) -> None:
    async def run() -> int:
        async with await connect(schema) as conn:
            await query_unchecked(t"SET client_encoding = {alias:ident}").execute(conn)
            return await query[int](t"SELECT 1").one(conn)

    assert asyncio.run(run()) == 1


def test_a_change_undone_inside_one_statement_is_never_reported_and_keeps_the_connection(schema: str) -> None:
    async def run() -> int:
        async with await connect(schema) as conn:
            # the server reports a changed parameter once per statement, and only if it differs from the last report
            await query_unchecked(
                t"DO $$ BEGIN SET client_encoding = 'SJIS'; SET client_encoding = 'UTF8'; END $$"
            ).execute(conn)
            return await query[int](t"SELECT 1").one(conn)

    assert asyncio.run(run()) == 1


def test_the_taint_survives_reset_all_and_a_later_utf8(schema: str) -> None:
    async def run() -> None:
        async with await connect(schema) as conn:
            await drift(conn, t"SET client_encoding = 'LATIN1'", "LATIN1")
            with pytest.raises(InterfaceError, match="LATIN1"):
                await query(t"RESET ALL").execute(conn)
            await assert_drifted(conn, "LATIN1")

    asyncio.run(run())


def test_set_local_client_encoding_in_a_transaction_taints(schema: str) -> None:
    async def run() -> None:
        async with await connect(schema) as conn:
            await query(t"BEGIN").execute(conn)
            await drift(conn, t"SET LOCAL client_encoding = 'SJIS'", "SJIS")
            await assert_drifted(conn, "SJIS")

    asyncio.run(run())


def test_rows_of_the_drifting_statement_fail_for_one(schema: str) -> None:
    async def run() -> None:
        async with await connect(schema) as conn:
            with pytest.raises(InterfaceError, match="changed client_encoding to LATIN1"):
                await query[tuple[str, str]](DRIFTING_ROWS).one(conn)
            await assert_drifted(conn, "LATIN1")

    asyncio.run(run())


def test_rows_of_the_drifting_statement_fail_for_all(schema: str) -> None:
    async def run() -> None:
        async with await connect(schema) as conn:
            with pytest.raises(InterfaceError, match="changed client_encoding to LATIN1"):
                await query[tuple[str, str]](DRIFTING_ROWS).all(conn)
            await assert_drifted(conn, "LATIN1")

    asyncio.run(run())


def test_rows_of_the_drifting_statement_fail_for_a_fetch_stream(schema: str) -> None:
    async def run() -> None:
        async with await connect(schema) as conn:
            with pytest.raises(InterfaceError, match="changed client_encoding to LATIN1"):
                _ = [row async for row in query[tuple[str, str]](DRIFTING_ROWS).fetch(conn)]
            await assert_drifted(conn, "LATIN1")

    asyncio.run(run())


def test_a_body_exception_is_kept_when_the_rollback_fails_on_a_drifted_connection(schema: str) -> None:
    async def run() -> None:
        async with await connect(schema) as conn:
            with pytest.raises(ValueError, match="body") as raised:  # ruff: ignore[pytest-raises-with-multiple-statements]
                async with conn.transaction():
                    await drift(conn, t"SET client_encoding = 'SJIS'", "SJIS")
                    msg = "body"
                    raise ValueError(msg)
            assert any("rollback also failed" in note and "SJIS" in note for note in raised.value.__notes__)
            await assert_drifted(conn, "SJIS")

    asyncio.run(run())


def test_a_clean_exit_on_a_drifted_connection_raises_the_drift_error(schema: str) -> None:
    async def run() -> None:
        async with await connect(schema) as conn:
            with pytest.raises(InterfaceError, match="changed client_encoding to SJIS"):
                async with conn.transaction():
                    await drift(conn, t"SET client_encoding = 'SJIS'", "SJIS")
            await assert_drifted(conn, "SJIS")

    asyncio.run(run())


@pytest.fixture
def lax_strings_role(schema: str) -> Iterator[str]:
    role = f"rowx_scs_{secrets.token_hex(4)}"
    schema_name = str(conninfo_to_dict(schema)["options"]).rsplit("=", 1)[1]
    with psycopg.connect(schema, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD 'x'").format(sql.Identifier(role)))
        admin.execute(sql.SQL("ALTER ROLE {} SET standard_conforming_strings = off").format(sql.Identifier(role)))
        admin.execute(sql.SQL("GRANT ALL ON SCHEMA {} TO {}").format(sql.Identifier(schema_name), sql.Identifier(role)))
        try:
            yield make_conninfo(schema, user=role, password="x")  # ruff: ignore[hardcoded-password-func-arg]
        finally:
            admin.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def test_a_role_default_of_standard_conforming_strings_off_fails_the_first_call(lax_strings_role: str) -> None:
    async def run() -> None:
        async with await connect(lax_strings_role) as conn:
            await assert_drifted(conn, "standard_conforming_strings")

    asyncio.run(run())


def _with_password(dsn: str, password: str) -> str:
    return make_conninfo(dsn, password=password)


def test_connect_and_close(schema: str) -> None:
    async def run() -> tuple[bool, bool, bool]:
        conn = await connect(schema)
        before = conn.closed
        await conn.close()
        await conn.close()
        return before, conn.closed, isinstance(conn, Connection)

    assert asyncio.run(run()) == (False, True, True)


def test_async_with_closes(schema: str) -> None:
    async def run() -> tuple[bool, bool]:
        async with await connect(ConnectOptions.from_dsn(schema, connect_timeout=5, statement_cache_size=0)) as conn:
            inside = conn.closed
        return inside, conn.closed

    assert asyncio.run(run()) == (False, True)


def test_malformed_dsn_is_interface_error() -> None:
    async def run() -> None:
        await connect("postgresql://[bad")

    with pytest.raises(InterfaceError):
        asyncio.run(run())


def test_negative_timeout_is_interface_error() -> None:
    async def run() -> None:
        await connect(ConnectOptions.from_dsn("postgresql://localhost/x", connect_timeout=-1))

    with pytest.raises(InterfaceError):
        asyncio.run(run())


def test_bad_password_is_database_error(schema: str) -> None:
    async def run() -> None:
        await connect(_with_password(schema, "definitely-wrong"))

    with pytest.raises(DatabaseError) as info:
        asyncio.run(run())
    assert info.value.sqlstate == "28P01"


def test_connect_in_task(schema: str) -> None:
    async def run() -> bool:
        conn = await asyncio.create_task(connect(schema))
        await conn.close()
        return conn.closed

    assert asyncio.run(run())


def test_close_is_coroutine_function() -> None:
    assert inspect.iscoroutinefunction(Connection.close)
    assert inspect.iscoroutinefunction(connect)


CERTS: Final[Path] = Path(
    os.environ.get(
        "ROWX_TEST_CERTS",
        next(parent for parent in Path(__file__).resolve().parents if (parent / "Cargo.lock").is_file())
        / ".rowx-certs",
    )
)
GONE_WITHIN: Final[float] = 2.0
SSL_IN_USE: Final[str] = "SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()"
SLEEPING_PG_SLEEP_3: Final[str] = (
    "SELECT count(*)::int4 FROM pg_stat_activity "
    "WHERE state = 'active' AND query LIKE '%pg_sleep(3)%' AND pid <> pg_backend_pid()"
)


def _require_env(name: str) -> str:
    if (value := os.environ.get(name)) is not None:
        return value
    if os.environ.get("ROWX_REQUIRE_DB") == "1":
        pytest.fail(f"ROWX_REQUIRE_DB=1 but {name} is not set")
    pytest.skip(f"{name} is not set")


@pytest.fixture
def ssl_dsn() -> str:
    return _require_env("ROWX_TEST_DSN_SSL")


def _kv(dsn: str, *, host: str | None = None, **extra: str) -> str:
    parts = urlparse(dsn)
    pairs = {
        "host": host or parts.hostname or "127.0.0.1",
        "port": str(parts.port or 5432),
        "user": parts.username or "",
        "password": parts.password or "",
        "dbname": parts.path.lstrip("/"),
        **extra,
    }
    if host is not None:
        pairs["hostaddr"] = parts.hostname or "127.0.0.1"
    return " ".join(f"{key}='{value}'" for key, value in pairs.items())


async def _fetch(conn: Connection, sql: str, *, scalar: bool = False) -> list[object]:
    mask = [False] if scalar else None
    return await conn._impl._fetch(sql, [], mask, scalar, scalar, 1 if scalar else None, None)


async def _scalar(conn: Connection, sql: str) -> object:
    (value,) = await _fetch(conn, sql, scalar=True)
    return value


async def _connect_and_check(dsn: str) -> object:
    conn = await connect(dsn)
    try:
        return await _scalar(conn, SSL_IN_USE)
    finally:
        await conn.close()


def _connect_error(dsn: str) -> None:
    with pytest.raises(InterfaceError):
        asyncio.run(_connect_and_check(dsn))


def test_verify_full_with_the_right_ca_succeeds(ssl_dsn: str) -> None:
    dsn = _kv(ssl_dsn, host="localhost", sslmode="verify-full", sslrootcert=str(CERTS / "ca.crt"))
    assert asyncio.run(_connect_and_check(dsn)) is True


def test_verify_full_with_the_wrong_ca_fails(ssl_dsn: str) -> None:
    dsn = _kv(ssl_dsn, host="localhost", sslmode="verify-full", sslrootcert=str(CERTS / "other-ca.crt"))
    _connect_error(dsn)


def test_verify_full_against_the_wrong_host_fails(ssl_dsn: str) -> None:
    dsn = _kv(ssl_dsn, host="wrong-host.example", sslmode="verify-full", sslrootcert=str(CERTS / "ca.crt"))
    _connect_error(dsn)


def test_verify_ca_against_the_wrong_host_succeeds(ssl_dsn: str) -> None:
    dsn = _kv(ssl_dsn, host="wrong-host.example", sslmode="verify-ca", sslrootcert=str(CERTS / "ca.crt"))
    assert asyncio.run(_connect_and_check(dsn)) is True


def test_verify_ca_with_the_wrong_ca_fails(ssl_dsn: str) -> None:
    dsn = _kv(ssl_dsn, sslmode="verify-ca", sslrootcert=str(CERTS / "other-ca.crt"))
    _connect_error(dsn)


def test_require_succeeds_without_a_ca(ssl_dsn: str) -> None:
    assert asyncio.run(_connect_and_check(_kv(ssl_dsn, sslmode="require"))) is True


def test_require_with_a_ca_verifies_the_chain(ssl_dsn: str) -> None:
    _connect_error(_kv(ssl_dsn, sslmode="require", sslrootcert=str(CERTS / "other-ca.crt")))


def test_prefer_encrypts_when_the_server_supports_it(ssl_dsn: str) -> None:
    assert asyncio.run(_connect_and_check(ssl_dsn)) is True


def test_prefer_against_a_non_ssl_server_falls_back_to_plaintext() -> None:
    dsn = _require_env("ROWX_TEST_DSN")

    async def run() -> object:
        conn = await connect(dsn)
        try:
            if await _scalar(conn, "SELECT current_setting('ssl')::bool"):
                pytest.skip("ROWX_TEST_DSN points at an SSL server")
            return await _scalar(conn, SSL_IN_USE)
        finally:
            await conn.close()

    assert asyncio.run(run()) is False


def test_disable_is_plaintext(ssl_dsn: str) -> None:
    assert asyncio.run(_connect_and_check(_kv(ssl_dsn, sslmode="disable"))) is False


@pytest.mark.parametrize("key", ["sslcert", "sslkey", "sslcrl"])
def test_client_certificate_settings_raise_interface_error(ssl_dsn: str, key: str) -> None:
    _connect_error(_kv(ssl_dsn, **{key: "/nonexistent"}))


def test_unreadable_sslrootcert_raises_interface_error(ssl_dsn: str) -> None:
    _connect_error(_kv(ssl_dsn, sslmode="verify-full", sslrootcert="/nonexistent/ca.pem"))


@pytest.mark.timing
def test_cancellation_works_over_tls(ssl_dsn: str) -> None:
    dsn = _kv(ssl_dsn, host="localhost", sslmode="verify-full", sslrootcert=str(CERTS / "ca.crt"))

    async def sleeping(observer: Connection) -> int:
        count = await _scalar(observer, SLEEPING_PG_SLEEP_3)
        assert isinstance(count, int)
        return count

    async def wait_for_sleep(observer: Connection, *, present: bool) -> bool:
        # each poll is a round trip on the observer, so no sleep is needed
        loop = asyncio.get_running_loop()
        deadline = loop.time() + GONE_WITHIN
        while loop.time() < deadline:
            if bool(await sleeping(observer)) == present:
                return True
        return False

    async def sleep_three(conn: Connection) -> list[object]:
        return await _fetch(conn, "SELECT pg_sleep(3)")

    async def run() -> object:
        conn = await connect(dsn)
        observer = await connect(dsn)
        try:
            slow = asyncio.create_task(sleep_three(conn))
            assert await wait_for_sleep(observer, present=True)
            slow.cancel()
            with pytest.raises(asyncio.CancelledError):
                await slow
            assert await wait_for_sleep(observer, present=False)
            return await _scalar(conn, "SELECT 1")
        finally:
            await conn.close()
            await observer.close()

    assert asyncio.run(run()) == 1


SLEEPING_PG_SLEEP_1: Final[str] = (
    "SELECT count(*)::int4 FROM pg_stat_activity WHERE query LIKE '%pg_sleep(1)%' AND pid <> pg_backend_pid()"
)


async def fetch(conn: ConnectionImpl, sql: str, *, scalar: bool = False) -> list[object]:
    mask = [False] if scalar else None
    return await conn._fetch(sql, [], mask, scalar, scalar, 1 if scalar else None, None)


async def select_one(conn: ConnectionImpl) -> list[object]:
    return await fetch(conn, "SELECT 1", scalar=True)


async def sleep_one(conn: ConnectionImpl) -> list[object]:
    return await fetch(conn, "SELECT pg_sleep(1)")


async def wait_until_running(observer: ConnectionImpl) -> None:
    # each poll is a round trip on the observer, so no sleep is needed
    for _ in range(POLL_LIMIT):
        (count,) = await fetch(observer, SLEEPING_PG_SLEEP_1, scalar=True)
        if count:
            return
    pytest.fail("the sleeping query never showed up in pg_stat_activity")


@pytest.mark.timing
def test_second_call_while_busy_raises_then_next_call_succeeds(schema: str) -> None:
    async def run() -> list[object]:
        conn = await connect(schema)
        observer = await connect(schema)
        try:
            slow = asyncio.create_task(sleep_one(conn._impl))
            await wait_until_running(observer._impl)
            with pytest.raises(ConnectionBusyError):
                await select_one(conn._impl)
            await slow
            return await select_one(conn._impl)
        finally:
            await conn.close()
            await observer.close()

    assert asyncio.run(run()) == [1]


@pytest.mark.timing
def test_sequential_awaits_never_raise_busy(schema: str) -> None:
    async def run() -> int:
        conn = await connect(schema)
        try:
            for _ in range(1000):
                await select_one(conn._impl)
                await select_one(conn._impl)
            return 1000
        finally:
            await conn.close()

    assert asyncio.run(run()) == 1000


@pytest.mark.timing
def test_error_result_leaves_the_connection_idle(schema: str) -> None:
    async def run() -> list[object]:
        conn = await connect(schema)
        try:
            with pytest.raises(DatabaseError):
                await fetch(conn._impl, "SELECT 1/0")
            return await select_one(conn._impl)
        finally:
            await conn.close()

    assert asyncio.run(run()) == [1]


PREPARED_COUNT: Final[str] = "SELECT count(*)::int4 FROM pg_prepared_statements"


async def fetch_rows(conn: ConnectionImpl, sql: str, mask: list[bool]) -> list[object]:
    scalar = limit_one = False
    return await conn._fetch(sql, [], mask, scalar, limit_one, len(mask), None)


async def prepared_count(conn: ConnectionImpl) -> int:
    (count,) = await fetch(conn, PREPARED_COUNT, scalar=True)
    assert isinstance(count, int)
    return count


def run_with_conn[T](dsn: str, body: Callable[[ConnectionImpl], Awaitable[T]], *, cache_size: int = 256) -> T:
    async def run() -> T:
        conn = await connect(ConnectOptions.from_dsn(dsn, statement_cache_size=cache_size))
        try:
            return await body(conn._impl)
        finally:
            await conn.close()

    return asyncio.run(run())


def test_cache_is_bounded(schema: str) -> None:
    async def body(c: ConnectionImpl) -> int:
        for i in range(300):
            await fetch(c, f"SELECT {i}")
        return await prepared_count(c)

    assert run_with_conn(schema, body) <= 256


def test_cache_size_zero_keeps_nothing_prepared(schema: str) -> None:
    async def body(c: ConnectionImpl) -> int:
        for i in range(5):
            await fetch(c, f"SELECT {i}")
        return await prepared_count(c)

    assert run_with_conn(schema, body, cache_size=0) <= 1


def test_schema_change_retries_outside_a_transaction(schema: str) -> None:
    async def body(c: ConnectionImpl) -> list[object]:
        await c._execute("CREATE TABLE cached_t (a int4)", [])
        await c._execute("INSERT INTO cached_t VALUES (1)", [])
        assert await fetch(c, "SELECT * FROM cached_t") == [(1,)]
        await c._execute("ALTER TABLE cached_t ADD COLUMN b int4 DEFAULT 2", [])
        return await fetch(c, "SELECT * FROM cached_t")

    assert run_with_conn(schema, body) == [(1, 2)]


def test_schema_change_reprepares_before_rejecting_a_new_row_shape(schema: str) -> None:
    async def body(c: ConnectionImpl) -> list[object]:
        sql = "SELECT * FROM cached_t"
        await c._execute("CREATE TABLE cached_t (a int4)", [])
        await c._execute("INSERT INTO cached_t VALUES (1)", [])
        assert await fetch_rows(c, sql, [False]) == [(1,)]
        await c._execute("ALTER TABLE cached_t ADD COLUMN b int4 DEFAULT 2", [])
        return await fetch_rows(c, sql, [False, False])

    assert run_with_conn(schema, body) == [(1, 2)]


def test_schema_change_inside_a_transaction_raises_with_hint(schema: str) -> None:
    async def body(c: ConnectionImpl) -> DatabaseError:
        await c._execute("CREATE TABLE cached_t (a int4)", [])
        await fetch(c, "SELECT * FROM cached_t")
        await c._execute("BEGIN", [])
        await c._execute("ALTER TABLE cached_t ADD COLUMN b int4", [])
        try:
            with pytest.raises(DatabaseError) as info:
                await fetch(c, "SELECT * FROM cached_t")
        finally:
            await c._execute("ROLLBACK", [])
        return info.value

    err = run_with_conn(schema, body)
    assert err.sqlstate == "0A000"
    assert err.hint == "schema changed inside a transaction; ROLLBACK and retry"


def test_discard_all_clears_the_cache_and_retries(schema: str) -> None:
    async def body(c: ConnectionImpl) -> list[object]:
        assert await fetch(c, "SELECT 1") == [(1,)]
        await c._execute("DISCARD ALL", [])
        return await fetch(c, "SELECT 1")

    assert run_with_conn(schema, body) == [(1,)]


STRESS_ITERATIONS: Final[int] = 300


async def sleeping(observer: ConnectionImpl) -> int:
    (count,) = await fetch(observer, SLEEPING_PG_SLEEP_3, scalar=True)
    assert isinstance(count, int)
    return count


async def wait_for_impl_sleep(observer: ConnectionImpl, *, present: bool, limit: float) -> bool:
    # each poll is a round trip on the observer, so no sleep is needed
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if bool(await sleeping(observer)) == present:
            return True
    return False


@pytest.mark.timing
def test_cancelled_sleep_frees_the_backend_and_the_next_query_succeeds(schema: str) -> None:
    async def run() -> list[object]:
        conn = await connect(schema)
        observer = await connect(schema)
        try:
            slow = asyncio.create_task(fetch(conn._impl, "SELECT pg_sleep(3)"))
            assert await wait_for_impl_sleep(observer._impl, present=True, limit=GONE_WITHIN)
            slow.cancel()
            with pytest.raises(asyncio.CancelledError):
                await slow
            assert await wait_for_impl_sleep(observer._impl, present=False, limit=GONE_WITHIN)
            return await select_one(conn._impl)
        finally:
            await conn.close()
            await observer.close()

    assert asyncio.run(run()) == [1]


@pytest.mark.timing
def test_timeout_cancel_after_100ms(schema: str) -> None:
    async def run() -> list[object]:
        conn = await connect(schema)
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(fetch(conn._impl, "SELECT pg_sleep(3)"), 0.1)
            return await select_one(conn._impl)
        finally:
            await conn.close()

    assert asyncio.run(run()) == [1]


@pytest.mark.timing
def test_stress_cancel_then_query_never_fails_with_57014(schema: str) -> None:
    async def run() -> list[str]:
        conn = await connect(schema)
        failures: list[str] = []
        rng = random.Random(22)  # ruff: ignore[suspicious-non-cryptographic-random-usage]
        try:
            for _ in range(STRESS_ITERATIONS):
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(fetch(conn._impl, "SELECT pg_sleep(0.004)"), rng.uniform(0.002, 0.006))
                try:
                    await select_one(conn._impl)
                except DatabaseError as exc:
                    failures.append(exc.sqlstate)
            return failures
        finally:
            await conn.close()

    assert asyncio.run(run()) == []


@pytest.mark.timing
def test_sql_error_sends_no_cancel_request(schema: str) -> None:
    async def run() -> int:
        conn = await connect(schema)
        try:
            before = _cancel_requests_sent()
            with pytest.raises(DatabaseError):
                await fetch(conn._impl, "SELECT 1/0")
            await select_one(conn._impl)
            return _cancel_requests_sent() - before
        finally:
            await conn.close()

    assert asyncio.run(run()) == 0


@pytest.mark.timing
def test_cancelling_a_large_fetch_leaves_the_connection_usable(schema: str) -> None:
    async def run() -> list[object]:
        conn = await connect(schema)
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(fetch(conn._impl, "SELECT g FROM generate_series(1, 1000000) g"), 0.01)
            return await select_one(conn._impl)
        finally:
            await conn.close()

    assert asyncio.run(run()) == [1]


@pytest.mark.timing
def test_close_during_a_long_query_returns_quickly(schema: str) -> None:
    async def run() -> float:
        conn = await connect(schema)
        observer = await connect(schema)
        try:
            slow = asyncio.create_task(fetch(conn._impl, "SELECT pg_sleep(3)"))
            assert await wait_for_impl_sleep(observer._impl, present=True, limit=GONE_WITHIN)
            started = time.monotonic()
            await conn.close()
            elapsed = time.monotonic() - started
            with pytest.raises(DatabaseError):
                await slow
            return elapsed
        finally:
            await observer.close()

    assert asyncio.run(run()) < GONE_WITHIN


async def wait_for_query_sleep(observer: Connection, *, present: bool, limit: float) -> bool:
    # each poll is a round trip on the observer, so no sleep is needed
    deadline = time.monotonic() + limit
    probe = query[int](
        t"SELECT count(*)::int4 FROM pg_stat_activity "
        t"WHERE state = 'active' AND query LIKE '%pg_sleep(3)%' AND pid <> pg_backend_pid()",
    )
    while time.monotonic() < deadline:
        if bool(await probe.one(observer)) == present:
            return True
    return False


async def consume_slow(conn: Connection) -> None:
    slow = query[str](t"SELECT pg_sleep(3)::text")
    async for _ in slow.fetch(conn):
        pass


@pytest.mark.timing
def test_cancelled_anext_on_slow_query_frees_the_backend(schema: str) -> None:
    async def run() -> int:
        conn = await connect(schema)
        observer = await connect(schema)
        try:
            task = asyncio.create_task(consume_slow(conn))
            assert await wait_for_query_sleep(observer, present=True, limit=GONE_WITHIN)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert await wait_for_query_sleep(observer, present=False, limit=GONE_WITHIN)
            return await query[int](t"SELECT 1").one(conn)
        finally:
            await conn.close()
            await observer.close()

    assert asyncio.run(run()) == 1


@pytest.mark.timing
def test_cancelled_stream_that_stays_referenced_ends_and_frees_the_connection(schema: str) -> None:
    async def run() -> tuple[int, bool]:
        conn = await connect(schema)
        observer = await connect(schema)
        try:
            stream = query[str](t"SELECT pg_sleep(3)::text").fetch(conn)
            task = asyncio.create_task(anext(stream))
            assert await wait_for_query_sleep(observer, present=True, limit=GONE_WITHIN)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert await wait_for_query_sleep(observer, present=False, limit=GONE_WITHIN)
            # the stream is still referenced, yet the connection is usable and the stream is over
            ok = await query[int](t"SELECT 1").one(conn)
            with pytest.raises(StopAsyncIteration):
                await anext(stream)
            return ok, stream.result is None
        finally:
            await conn.close()
            await observer.close()

    assert asyncio.run(run()) == (1, True)


@pytest.mark.timing
def test_close_during_a_pending_anext_raises_interface_error(schema: str) -> None:
    async def run() -> None:
        conn = await connect(schema)
        observer = await connect(schema)
        try:
            pending = asyncio.create_task(anext(query[str](t"SELECT pg_sleep(3)::text").fetch(conn)))
            assert await wait_for_query_sleep(observer, present=True, limit=GONE_WITHIN)
            await conn.close()
            with pytest.raises(InterfaceError):
                await pending
        finally:
            await observer.close()

    asyncio.run(run())


@pytest.mark.timing
def test_stream_cancel_stress_follow_up_query_never_fails(schema: str) -> None:
    async def one_stream(conn: Connection) -> None:
        async with query[int](t"SELECT g FROM generate_series(1, 200000) g").fetch(conn, batch_size=50) as rows:
            async for _ in rows:
                pass

    async def run() -> list[str]:
        conn = await connect(schema)
        failures: list[str] = []
        rng = random.Random(63)  # ruff: ignore[suspicious-non-cryptographic-random-usage]
        try:
            for _ in range(STRESS_ITERATIONS):
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(one_stream(conn), rng.uniform(0.0005, 0.02))
                try:
                    await query[int](t"SELECT 1").one(conn)
                except rowx.Error as exc:
                    failures.append(repr(exc))
            return failures
        finally:
            await conn.close()

    assert asyncio.run(run()) == []
