import asyncio
from typing import TYPE_CHECKING

import pytest

import rowx
from rowx import query
from rowx.errors import EncodeError, InterfaceError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

pytestmark = [pytest.mark.integration, pytest.mark.db]


def run_with_conn[T](dsn: str, body: Callable[[rowx.Connection], Awaitable[T]]) -> T:
    async def run() -> T:
        async with await rowx.connect(dsn) as conn:
            return await body(conn)

    return asyncio.run(run())


def test_int_round_trip(schema: str) -> None:
    x = 7
    assert run_with_conn(schema, query[int](t"SELECT {x}::int").fetch_one) == 7


def test_int8_extremes(schema: str) -> None:
    lo, hi = -(2**63), 2**63 - 1
    q = query[tuple[int, int]](t"SELECT {lo}::int8, {hi}::int8")
    assert run_with_conn(schema, q.fetch_one) == (lo, hi)


def test_bool_and_text_round_trip(schema: str) -> None:
    flag, word = True, "héllo ✓"
    q = query[tuple[bool, str]](t"SELECT {flag}::bool, {word}::text")
    assert run_with_conn(schema, q.fetch_one) == (flag, word)


def test_none_is_null(schema: str) -> None:
    missing = None
    q = query[int | None](t"SELECT {missing}::int")
    assert run_with_conn(schema, q.fetch_one) is None


def test_two_interpolations_bind_two_parameters(schema: str) -> None:
    a, b = 1, "b"
    q = query[tuple[int, str]](t"SELECT {a}::int, {b}::text")
    assert run_with_conn(schema, q.fetch_one) == (1, "b")


def test_execute_with_params(schema: str) -> None:
    n = 3
    assert run_with_conn(schema, query(t"SELECT {n}::int").execute) == 1


def test_int2_out_of_range_names_the_parameter(schema: str) -> None:
    x = 40000
    with pytest.raises(EncodeError, match=r"\$1.*int2"):
        run_with_conn(schema, query[int](t"SELECT {x}::int2").fetch_one)


def test_int8_overflow_is_encode_error(schema: str) -> None:
    x = 2**63
    with pytest.raises(EncodeError, match=r"\$1"):
        run_with_conn(schema, query[int](t"SELECT {x}::int8").fetch_one)


def test_bool_is_not_an_int(schema: str) -> None:
    x = True
    with pytest.raises(EncodeError, match=r"\$1.*bool.*int4"):
        run_with_conn(schema, query[int](t"SELECT {x}::int4").fetch_one)


def test_int_is_not_text(schema: str) -> None:
    x = 42
    with pytest.raises(EncodeError, match=r"\$1.*int.*text"):
        run_with_conn(schema, query[str](t"SELECT {x}::text").fetch_one)


def test_nul_in_text_is_encode_error(schema: str) -> None:
    x = "a\0b"
    with pytest.raises(EncodeError, match=r"\$1"):
        run_with_conn(schema, query[str](t"SELECT {x}::text").fetch_one)


def test_interpolation_inside_literal_is_count_mismatch(schema: str) -> None:
    x = 1
    with pytest.raises(InterfaceError, match="parameters"):
        run_with_conn(schema, query[str](t"SELECT '{x}'").fetch_one)


def test_connection_usable_after_encode_error(schema: str) -> None:
    async def body(conn: rowx.Connection) -> int:
        x = 40000
        with pytest.raises(EncodeError):
            await query[int](t"SELECT {x}::int2").fetch_one(conn)
        return await query[int](t"SELECT 1").fetch_one(conn)

    assert run_with_conn(schema, body) == 1
