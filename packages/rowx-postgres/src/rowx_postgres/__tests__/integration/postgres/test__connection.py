import asyncio
import contextlib
import re
import secrets
from typing import TYPE_CHECKING, Final

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from rowx_core.errors import ConnectionBusyError, DatabaseError, InterfaceError
from rowx_postgres import RESET_QUERY, Connection, connect

from rowx import query, query_unchecked

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator
    from string.templatelib import Template

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
