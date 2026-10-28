import asyncio
import secrets
from typing import TYPE_CHECKING, Any, Final

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from rowx.__tests__.integration.describe.test_base import run_describe
from rowx.check.compare import ColumnDesc, DescribeDesc, compare
from rowx.check.resolve import Elem, RowType
from rowx.errors import DecodeError
from rowx.postgres import Cube, connect

if TYPE_CHECKING:
    from collections.abc import Iterator

    from rowx._core.postgres import Describe

pytestmark = [pytest.mark.integration, pytest.mark.db]

# the type each extension provides, and the extension that provides it
EXTENSIONS: Final[dict[str, str]] = {
    "citext": "citext",
    "hstore": "hstore",
    "ltree": "ltree",
    "lquery": "ltree",
    "cube": "cube",
}
SAMPLES: Final[dict[str, Any]] = {
    "citext": "MiXed",
    "hstore": {"a": "1", "b": None},
    "ltree": "a.b.c",
    "lquery": "a.*",
    "cube": Cube((1.0,), (2.0,)),
}
FAKE_TYPE_SQL: Final[str] = """
CREATE TYPE other.{name};
CREATE FUNCTION other.{name}_in(cstring) RETURNS other.{name} AS 'textin' LANGUAGE internal IMMUTABLE STRICT;
CREATE FUNCTION other.{name}_out(other.{name}) RETURNS cstring AS 'textout' LANGUAGE internal IMMUTABLE STRICT;
CREATE FUNCTION other.{name}_recv(internal) RETURNS other.{name} AS 'textrecv' LANGUAGE internal IMMUTABLE STRICT;
CREATE FUNCTION other.{name}_send(other.{name}) RETURNS bytea AS 'textsend' LANGUAGE internal IMMUTABLE STRICT;
CREATE TYPE other.{name} (
    INPUT = other.{name}_in, OUTPUT = other.{name}_out, RECEIVE = other.{name}_recv, SEND = other.{name}_send,
    LIKE = text
);
"""


@pytest.fixture
def scratch(schema: str) -> Iterator[str]:
    # an extension installs once per database, so the schema-placement cases need a database of their own
    info: dict[str, Any] = {key: value for key, value in conninfo_to_dict(schema).items() if key != "options"}
    name = f"rowx_ext_{secrets.token_hex(6)}"
    with psycopg.connect(make_conninfo(**info), autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"'.encode())
        try:
            dsn = make_conninfo(**{**info, "dbname": name})
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute(b"CREATE SCHEMA ext; CREATE SCHEMA other")
            yield dsn
        finally:
            admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)'.encode())


def sql(dsn: str, statement: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(statement.encode())


def install(dsn: str, extension: str, schema: str = "ext") -> None:
    sql(dsn, f'CREATE EXTENSION "{extension}" SCHEMA {schema}')


def fake(dsn: str, name: str) -> None:
    sql(dsn, FAKE_TYPE_SQL.format(name=name))


def column(described: Describe) -> ColumnDesc:
    (info,) = described.columns
    return ColumnDesc(
        name=info.name,
        type_name=info.type_name,
        type_key=info.type_key,
        nullable=info.nullable,
        element_key=info.element_key,
    )


def finding_rules(described: Describe, leaf: str) -> list[str]:
    row_type = RowType(kind="scalar", elements=(Elem(leaf=leaf, nullable=True),))  # ty: ignore[invalid-argument-type]
    return [found.rule for found in compare(DescribeDesc(columns=(column(described),)), row_type)]


def fetch(dsn: str, statement: str, params: list[object]) -> object:
    async def run() -> object:
        async with await connect(dsn) as conn:
            return (await conn._impl._fetch(statement, params, None, scalar=True, limit_one=True, arity=1))[0]

    return asyncio.run(run())


@pytest.mark.parametrize("name", sorted(EXTENSIONS))
def test_extension_type_in_a_non_public_schema_resolves(scratch: str, name: str) -> None:
    install(scratch, EXTENSIONS[name])
    described = run_describe(scratch, f"SELECT NULL::ext.{name}")
    assert [c.type_key for c in described.columns] == [name]
    param = run_describe(scratch, f"SELECT $1::ext.{name}")
    assert [p.type_key for p in param.params] == [name]
    array = run_describe(scratch, f"SELECT $1::ext.{name}[]")
    assert [(p.type_key, p.element_key) for p in array.params] == [("array", name)]


@pytest.mark.parametrize("name", sorted(EXTENSIONS))
def test_extension_type_in_a_non_public_schema_round_trips(scratch: str, name: str) -> None:
    install(scratch, EXTENSIONS[name])
    value = SAMPLES[name]
    assert fetch(scratch, f"SELECT $1::ext.{name}", [value]) == value
    assert fetch(scratch, f"SELECT $1::ext.{name}[]", [[value, None]]) == [value, None]


@pytest.mark.parametrize("name", sorted(EXTENSIONS))
def test_a_same_named_user_type_is_unsupported_when_the_extension_is_missing(scratch: str, name: str) -> None:
    fake(scratch, name)
    described = run_describe(scratch, f"SELECT NULL::other.{name}")
    assert [c.type_key for c in described.columns] == [None]
    assert finding_rules(described, "str") == ["unsupported-type"]
    array = run_describe(scratch, f"SELECT NULL::other.{name}[]")
    assert [(c.type_key, c.element_key) for c in array.columns] == [(None, None)]
    param = run_describe(scratch, f"SELECT $1::other.{name}")
    assert [p.type_key for p in param.params] == [None]


@pytest.mark.parametrize("name", sorted(EXTENSIONS))
def test_a_same_named_user_type_does_not_get_the_extension_codec(scratch: str, name: str) -> None:
    install(scratch, EXTENSIONS[name])
    fake(scratch, name)
    assert [c.type_key for c in run_describe(scratch, f"SELECT NULL::ext.{name}").columns] == [name]
    assert [c.type_key for c in run_describe(scratch, f"SELECT NULL::other.{name}").columns] == [None]
    # both in one statement: each is judged by its own owner
    both = run_describe(scratch, f"SELECT NULL::ext.{name}, NULL::other.{name}")
    assert [c.type_key for c in both.columns] == [name, None]


@pytest.mark.parametrize("name", sorted(EXTENSIONS))
def test_runtime_refuses_a_same_named_user_type(scratch: str, name: str) -> None:
    install(scratch, EXTENSIONS[name])
    fake(scratch, name)
    with pytest.raises(DecodeError, match=rf"other\.{name}"):
        fetch(scratch, f"SELECT NULL::other.{name}", [])
    with pytest.raises(DecodeError, match=rf"other\.{name}"):
        fetch(scratch, f"SELECT $1::other.{name}", [None])
    with pytest.raises(DecodeError, match=rf"other\.{name}"):
        fetch(scratch, f"SELECT $1::other.{name}[]", [None])
    # the real type still works on a connection that has seen the refusal
    value = SAMPLES[name]
    assert fetch(scratch, f"SELECT $1::ext.{name}", [value]) == value


def test_runtime_refuses_a_same_named_user_type_when_the_extension_is_missing(scratch: str) -> None:
    fake(scratch, "hstore")
    with pytest.raises(DecodeError, match=r"other\.hstore"):
        fetch(scratch, "SELECT 'x'::other.hstore", [])


def test_a_same_named_type_owned_by_another_extension_is_unsupported(scratch: str) -> None:
    install(scratch, "ltree")
    install(scratch, "hstore", "other")
    fake(scratch, "cube")
    # `other.cube` belongs to ltree, not to the cube extension
    sql(scratch, "ALTER EXTENSION ltree ADD TYPE other.cube")
    described = run_describe(scratch, "SELECT NULL::other.cube")
    assert [c.type_key for c in described.columns] == [None]
    with pytest.raises(DecodeError, match=r"other\.cube"):
        fetch(scratch, "SELECT NULL::other.cube", [])
    # a type of an extension's own name still resolves from the extension that owns it
    assert [c.type_key for c in run_describe(scratch, "SELECT NULL::other.hstore").columns] == ["hstore"]
    assert [c.type_key for c in run_describe(scratch, "SELECT NULL::ext.ltree").columns] == ["ltree"]


def test_lquery_is_owned_by_the_ltree_extension_not_by_an_extension_of_its_own_name(scratch: str) -> None:
    install(scratch, "ltree")
    fake(scratch, "lquery")
    sql(scratch, "ALTER EXTENSION ltree ADD TYPE other.lquery")
    # owned by ltree, and named lquery, so it is the extension's lquery as far as the owner rule can tell
    assert [c.type_key for c in run_describe(scratch, "SELECT NULL::other.lquery").columns] == ["lquery"]


def test_domain_over_an_extension_type_follows_the_owner(scratch: str) -> None:
    install(scratch, "hstore")
    sql(scratch, "CREATE DOMAIN other.tags AS ext.hstore")
    described = run_describe(scratch, "SELECT NULL::other.tags")
    assert [c.type_key for c in described.columns] == ["hstore"]
    assert fetch(scratch, "SELECT $1::other.tags", [{"a": "1"}]) == {"a": "1"}
    fake(scratch, "ltree")
    sql(scratch, "CREATE DOMAIN other.paths AS other.ltree")
    assert [c.type_key for c in run_describe(scratch, "SELECT NULL::other.paths").columns] == [None]


def test_statement_cache_keeps_serving_a_statement_after_the_owner_check(scratch: str) -> None:
    install(scratch, "hstore")

    async def run() -> list[object]:
        async with await connect(scratch) as conn:
            return [
                (
                    await conn._impl._fetch(
                        "SELECT $1::ext.hstore", [{"k": str(i)}], None, scalar=True, limit_one=True, arity=1
                    )
                )[0]
                for i in range(5)
            ]

    assert asyncio.run(run()) == [{"k": str(i)} for i in range(5)]
