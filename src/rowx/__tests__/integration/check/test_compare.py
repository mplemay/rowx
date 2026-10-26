import ast
import asyncio
import datetime as dt
import ipaddress
from dataclasses import dataclass, replace
from decimal import Decimal
from string.templatelib import Interpolation, Template
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

import psycopg
import pytest

from rowx import query
from rowx.__tests__.integration.golden.test_table_coverage import DECODE_GOLDENS
from rowx._core.postgres import Describe, type_table
from rowx.check.compare import ColumnDesc, DescribeDesc, compare, compare_param
from rowx.check.params import ParamInference
from rowx.check.resolve import Elem, RowType
from rowx.check.runner import describe_all
from rowx.errors import EncodeError, Error, RowShapeError
from rowx.postgres import connect

if TYPE_CHECKING:
    from collections.abc import Callable

    from rowx._core.postgres import ColumnInfo
    from rowx.postgres._vocab import LeafName

pytestmark = [pytest.mark.integration, pytest.mark.db]

TIMEOUT_MS: Final[int] = 10_000
JSON_KEYS: Final[frozenset[str]] = frozenset({"json", "jsonb"})
TYPE_RULES: Final[frozenset[str]] = frozenset({"type-mismatch", "unsupported-type"})
# the table keys that are built from a golden by hand, because their SQL does not follow the `'lit'::type` shape
COMPOSITE_KEYS: Final[frozenset[str]] = frozenset({"array", "domain", "void"})


@dataclass(slots=True, kw_only=True, frozen=True)
class SqlType:
    name: str
    # the declared type, as it is written in DDL or a cast
    decl: str
    # an expression that yields one non-NULL value of the type
    expr: str
    # the table keys this entry exercises
    keys: frozenset[str]
    # 0 for a scalar, otherwise the number of dimensions of the value (the declared type does not record it)
    dims: int = 0
    element_key: str | None = None

    @property
    def statement(self) -> str:
        return f"SELECT {self.expr}"

    @property
    def json(self) -> bool:
        return (self.element_key or next(iter(self.keys))) in JSON_KEYS

    @property
    def void(self) -> bool:
        return self.keys == {"void"}


def scalar_types() -> list[SqlType]:
    found: list[SqlType] = []
    for row in type_table():
        if row.key in COMPOSITE_KEYS or row.key not in DECODE_GOLDENS:
            continue
        decl, literal, _ = DECODE_GOLDENS[row.key]
        found.append(SqlType(name=row.key, decl=decl, expr=f"'{literal}'::{decl}", keys=frozenset({row.key})))
    return found


def array_types(element: SqlType, key: str) -> list[SqlType]:
    value = element.expr
    return [
        SqlType(
            name=f"{element.name}[]",
            decl=f"{element.decl}[]",
            expr=f"ARRAY[{value}, NULL]::{element.decl}[]",
            keys=frozenset({"array", key}),
            dims=1,
            element_key=key,
        ),
        SqlType(
            name=f"{element.name}[][]",
            decl=f"{element.decl}[]",
            expr=f"ARRAY[ARRAY[{value}, NULL]]::{element.decl}[]",
            keys=frozenset({"array", key}),
            dims=2,
            element_key=key,
        ),
    ]


def sql_types() -> list[SqlType]:
    scalars = scalar_types()
    domain = SqlType(name="domain", decl="positive_int", expr="'5'::positive_int", keys=frozenset({"domain"}))
    void = SqlType(name="void", decl="void", expr="pg_sleep(0)", keys=frozenset({"void"}))
    found = [*scalars, domain, void]
    for element in [*scalars, domain]:
        if element.name == "void":
            continue
        found.extend(array_types(element, next(iter(element.keys))))
    # a domain over an array, which is its own table row's variant
    found.append(
        SqlType(
            name="domain_array",
            decl="positive_ints",
            expr="'{1,2}'::positive_ints",
            keys=frozenset({"domain", "array"}),
            dims=1,
            element_key="domain",
        ),
    )
    return found


type Check = Callable[[object], bool]


def is_list_of(inner: Check) -> Check:
    # an element at any nesting level may be NULL
    return lambda value: type(value) is list and all(item is None or inner(item) for item in value)


@dataclass(slots=True, kw_only=True, frozen=True)
class Leaf:
    name: str
    annotation: object
    elem: Elem
    is_value: Check
    depth: int = 0

    @property
    def neutral(self) -> bool:
        return self.name in {"Any", "object"}


def scalar_leaf(name: str, annotation: type, leaf: Any) -> Leaf:  # ruff: ignore[any-type]
    return Leaf(
        name=name,
        annotation=annotation,
        elem=Elem(leaf=leaf, nullable=False),
        is_value=lambda value: type(value) is annotation,
    )


def list_leaf(name: str, annotation: object, inner: Leaf, *, item_nullable: bool = False) -> Leaf:
    return Leaf(
        name=name,
        annotation=annotation,
        elem=Elem(leaf="list", nullable=False, item=replace(inner.elem, nullable=item_nullable)),
        is_value=is_list_of(inner.is_value),
        depth=inner.depth + 1,
    )


def pair_leaf(name: str, annotation: object, leaf: LeafName, members: tuple[type, ...]) -> Leaf:
    return Leaf(
        name=name,
        annotation=annotation,
        elem=Elem(leaf=leaf, nullable=False),
        is_value=lambda value: type(value) in members,
    )


INET_LEAF: Final[Leaf] = pair_leaf(
    "IPv4Interface | IPv6Interface",
    ipaddress.IPv4Interface | ipaddress.IPv6Interface,
    "ipaddress.IPv4Interface | ipaddress.IPv6Interface",
    (ipaddress.IPv4Interface, ipaddress.IPv6Interface),
)
CIDR_LEAF: Final[Leaf] = pair_leaf(
    "IPv4Network | IPv6Network",
    ipaddress.IPv4Network | ipaddress.IPv6Network,
    "ipaddress.IPv4Network | ipaddress.IPv6Network",
    (ipaddress.IPv4Network, ipaddress.IPv6Network),
)
INT_LEAF: Final[Leaf] = scalar_leaf("int", int, "int")
STR_LEAF: Final[Leaf] = scalar_leaf("str", str, "str")
LIST_INT: Final[Leaf] = list_leaf("list[int]", list[int], INT_LEAF)
LEAVES: Final[list[Leaf]] = [
    INT_LEAF,
    scalar_leaf("bool", bool, "bool"),
    scalar_leaf("float", float, "float"),
    STR_LEAF,
    scalar_leaf("bytes", bytes, "bytes"),
    scalar_leaf("Decimal", Decimal, "decimal.Decimal"),
    scalar_leaf("date", dt.date, "datetime.date"),
    scalar_leaf("time", dt.time, "datetime.time"),
    scalar_leaf("datetime", dt.datetime, "datetime.datetime"),
    scalar_leaf("timedelta", dt.timedelta, "datetime.timedelta"),
    scalar_leaf("UUID", UUID, "uuid.UUID"),
    INET_LEAF,
    CIDR_LEAF,
    LIST_INT,
    list_leaf("list[int | None]", list[int | None], INT_LEAF, item_nullable=True),
    list_leaf("list[str]", list[str], STR_LEAF),
    list_leaf("list[list[int]]", list[list[int]], LIST_INT),
    Leaf(name="Any", annotation=Any, elem=Elem(leaf="any", nullable=False), is_value=lambda _: True),
    Leaf(name="object", annotation=object, elem=Elem(leaf="any", nullable=False), is_value=lambda _: True),
    Leaf(name="None", annotation=object, elem=Elem(leaf="none", nullable=False), is_value=lambda value: value is None),
]
LEAF_BY_NAME: Final[dict[str, Leaf]] = {leaf.name: leaf for leaf in LEAVES}


def column_desc(info: ColumnInfo) -> ColumnDesc:
    return ColumnDesc(
        name=info.name,
        type_name=info.type_name,
        type_key=info.type_key,
        nullable=info.nullable,
        reason=info.reason,
        reason_detail=info.reason_detail,
        element_key=info.element_key,
    )


def findings_for(column: ColumnDesc, leaf: Leaf, *, nullable: bool) -> list[str]:
    row_type = RowType(kind="scalar", elements=(replace(leaf.elem, nullable=nullable),))
    return [finding.rule for finding in compare(DescribeDesc(columns=(column,)), row_type)]


def describe_columns(outcomes: dict[str, Any], sql: str) -> list[ColumnDesc]:
    described = outcomes[sql]
    assert isinstance(described, Describe), f"{sql}: {described}"
    return [column_desc(info) for info in described.columns]


# what the checker is documented to say regardless of the runtime value; `None` defers to the runtime oracle
def documented(sql: SqlType, leaf: Leaf) -> bool | None:
    # a `void` column matches only the tuple element `None`, even against `Any`
    if sql.void:
        return leaf.name == "None"
    if leaf.neutral:
        return True
    # json and jsonb match every leaf (but the bare `None`); an array of them matches any list leaf
    if sql.json:
        return leaf.depth > 0 if sql.dims else leaf.name != "None"
    return None


# the checker cannot see how many dimensions an array has, because the declared type does not record it, so it
# models every array as one-dimensional (#112); see `test_multidimensional_array_disagrees`
def dimension_limit(sql: SqlType, leaf: Leaf) -> bool:
    return sql.dims == 2 and leaf.depth > 0


def query_for(annotation: object, template: Template) -> Any:  # ruff: ignore[any-type]
    return query[annotation](template)  # ty: ignore[invalid-type-form]


async def run_values(dsn: str, types: list[SqlType]) -> dict[tuple[str, str], object]:
    results: dict[tuple[str, str], object] = {}
    async with await connect(dsn) as conn:
        for sql in types:
            for leaf in LEAVES:
                try:
                    results[sql.name, leaf.name] = await query_for(leaf.annotation, Template(sql.statement)).one(conn)
                except Error as error:
                    results[sql.name, leaf.name] = error
    return results


def test_matrix_covers_type_table() -> None:
    covered = {key for sql in sql_types() for key in sql.keys}
    missing = {row.key for row in type_table()} - covered
    assert not missing, f"the matrix has no SQL type for the table keys {sorted(missing)}"
    assert len({sql.name for sql in sql_types()}) == len(sql_types())


def test_result_matrix(schema: str) -> None:
    types = sql_types()
    outcomes = asyncio.run(describe_all(schema, [sql.statement for sql in types], 1, TIMEOUT_MS))
    values = asyncio.run(run_values(schema, types))
    disagreements: list[str] = []
    cells = 0
    for sql in types:
        (column,) = describe_columns(outcomes, sql.statement)
        for leaf in LEAVES:
            if dimension_limit(sql, leaf):
                continue
            cells += 1
            found = findings_for(column, leaf, nullable=column.nullable)
            checker_ok = not TYPE_RULES.intersection(found)
            value = values[sql.name, leaf.name]
            expected = documented(sql, leaf)
            if expected is None:
                expected = not isinstance(value, Error) and leaf.is_value(value)
            if checker_ok != expected:
                shown = value if isinstance(value, Error) else type(value).__name__
                disagreements.append(
                    f"{sql.name} x {leaf.name}: checker ok={checker_ok}, runtime ok={expected} ({shown})"
                )
    assert cells > len(types)
    assert not disagreements, "\n".join(disagreements)


def test_multidimensional_array_disagrees(schema: str) -> None:
    # a two-dimensional column decodes to nested lists, but the checker models every array as one-dimensional
    sql = next(sql for sql in sql_types() if sql.name == "int4[][]")
    outcomes = asyncio.run(describe_all(schema, [sql.statement], 1, TIMEOUT_MS))
    values = asyncio.run(run_values(schema, [sql]))
    (column,) = describe_columns(outcomes, sql.statement)
    flat, nested = LEAF_BY_NAME["list[int]"], LEAF_BY_NAME["list[list[int]]"]
    assert not TYPE_RULES.intersection(findings_for(column, flat, nullable=column.nullable))
    assert not flat.is_value(values[sql.name, flat.name])
    assert TYPE_RULES.intersection(findings_for(column, nested, nullable=column.nullable))
    assert nested.is_value(values[sql.name, nested.name])


# --- nullability ---


# the declared types a column can have; the declared type is the same for every dimension count
def column_types() -> list[SqlType]:
    unique: dict[str, SqlType] = {}
    for sql in sql_types():
        if not sql.void and sql.decl not in unique and sql.dims != 2:
            unique[sql.decl] = sql
    return list(unique.values())


def natural_leaf(column: ColumnDesc) -> Leaf:
    candidates = [leaf for leaf in LEAVES if not leaf.neutral and leaf.name != "None"]
    # the fixed leaves hold only a few array shapes, so every other one-dimensional array gets its own
    candidates += [
        list_leaf(f"list[{inner.name}]", list[inner.annotation], inner)  # ty: ignore[invalid-type-form]
        for inner in LEAVES[:13]
    ]
    for leaf in candidates:
        if not TYPE_RULES.intersection(findings_for(column, leaf, nullable=column.nullable)):
            return leaf
    msg = f"no leaf matches {column.type_name}"
    raise AssertionError(msg)


def create_tables(dsn: str, types: list[SqlType]) -> None:
    nullable = ", ".join(f"n{index} {sql.decl}" for index, sql in enumerate(types))
    not_null = ", ".join(f"c{index} {sql.decl} NOT NULL" for index, sql in enumerate(types))
    values = ", ".join(sql.expr for sql in types)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f"CREATE TABLE agree_nullable ({nullable})")  # ty: ignore[no-matching-overload]
        conn.execute("INSERT INTO agree_nullable DEFAULT VALUES")
        conn.execute(f"CREATE TABLE agree_not_null ({not_null})")  # ty: ignore[no-matching-overload]
        conn.execute(f"INSERT INTO agree_not_null SELECT {values}")  # ty: ignore[no-matching-overload]


async def run_nullability(dsn: str, cells: list[tuple[str, Leaf, bool]]) -> list[object]:
    outcomes: list[object] = []
    async with await connect(dsn) as conn:
        for sql, leaf, nullable_annotation in cells:
            annotation = leaf.annotation | None if nullable_annotation else leaf.annotation  # ty: ignore[unsupported-operator]
            try:
                outcomes.append(await query_for(annotation, Template(sql)).one(conn))
            except Error as error:
                outcomes.append(error)
    return outcomes


def test_nullability_matrix(schema: str) -> None:
    types = column_types()
    create_tables(schema, types)
    statements = [f"SELECT n{index} FROM agree_nullable" for index in range(len(types))]  # ruff: ignore[hardcoded-sql-expression]
    statements += [f"SELECT c{index} FROM agree_not_null" for index in range(len(types))]  # ruff: ignore[hardcoded-sql-expression]
    outcomes = asyncio.run(describe_all(schema, statements, 1, TIMEOUT_MS))
    columns = {sql: describe_columns(outcomes, sql)[0] for sql in statements}

    cells: list[tuple[str, Leaf, bool]] = []
    expected: list[tuple[bool, bool]] = []
    labels: list[str] = []
    for sql in statements:
        column = columns[sql]
        leaf = natural_leaf(column)
        for nullable_annotation in (False, True):
            cells.append((sql, leaf, nullable_annotation))
            # (the checker reports a mismatch, the runtime raises RowShapeError); a NOT NULL column annotated
            # `X | None` is flagged by the checker, which is stricter than the runtime, because it can never hold NULL
            expected.append((column.nullable != nullable_annotation, column.nullable and not nullable_annotation))
            labels.append(f"{sql} ({column.type_name}) as {leaf.name}{' | None' if nullable_annotation else ''}")
    runtime = asyncio.run(run_nullability(schema, cells))

    problems: list[str] = []
    for label, (sql, leaf, nullable_annotation), (want_finding, want_shape_error), outcome in zip(
        labels, cells, expected, runtime, strict=True
    ):
        column = columns[sql]
        found = "nullability-mismatch" in findings_for(column, leaf, nullable=nullable_annotation)
        shape_error = isinstance(outcome, RowShapeError)
        if found != want_finding:
            problems.append(f"{label}: checker finding={found}, expected {want_finding}")
        if shape_error != want_shape_error or (isinstance(outcome, Error) and not shape_error):
            problems.append(f"{label}: runtime raised {outcome!r}, expected RowShapeError={want_shape_error}")
        # the NOT NULL annotation direction: a finding there is a prediction of RowShapeError
        if column.nullable and not nullable_annotation and found != shape_error:
            problems.append(f"{label}: checker predicted {found} but the runtime {shape_error}")
    assert len(cells) == 4 * len(types)
    assert not problems, "\n".join(problems)


# --- parameters ---

PRELUDE: Final[str] = "import datetime\nimport decimal\nimport ipaddress\nimport uuid\n"
UUID_TEXT: Final[str] = "12345678-1234-5678-1234-567812345678"
# python source -> value; the source is what the checker's inference reads, and the value is what the runtime binds
PARAM_VALUES: Final[list[str]] = [
    "True",
    "1",
    "1.5",
    '"ok"',
    f'"{UUID_TEXT}"',
    'b"\\x00"',
    'bytearray(b"\\x00")',
    'memoryview(b"\\x00")',
    'decimal.Decimal("1")',
    f'uuid.UUID("{UUID_TEXT}")',
    "datetime.date(2024, 1, 2)",
    "datetime.datetime(2024, 1, 2, 3, 4, 5)",
    "datetime.datetime(2024, 1, 2, 3, 4, 5, tzinfo=datetime.UTC)",
    "datetime.timedelta(days=1)",
    "datetime.time(1, 2, 3)",
    'ipaddress.IPv4Interface("10.0.0.1/24")',
    'ipaddress.IPv6Interface("2001:db8::1/64")',
    'ipaddress.IPv4Address("10.0.0.1")',
    'ipaddress.IPv6Address("::ffff:1.2.3.4")',
    'ipaddress.IPv4Network("10.0.0.0/8")',
    'ipaddress.IPv6Network("2001:db8::/32")',
    "None",
    "[1, 2]",
    "(1, 2)",
    '["ok"]',
    "[1.5]",
    "[True]",
    "[None]",
    "[[1, 2], [3, 4]]",
]
NOT_JSON: Final[list[str]] = [
    'b"\\x00"',
    'decimal.Decimal("1")',
    f'uuid.UUID("{UUID_TEXT}")',
    "datetime.date(2024, 1, 2)",
    "datetime.datetime(2024, 1, 2, 3, 4, 5)",
    "datetime.datetime(2024, 1, 2, 3, 4, 5, tzinfo=datetime.UTC)",
    "datetime.timedelta(days=1)",
    'ipaddress.IPv4Interface("10.0.0.1/24")',
    'ipaddress.IPv6Interface("2001:db8::1/64")',
    'ipaddress.IPv4Address("10.0.0.1")',
    'ipaddress.IPv6Address("::ffff:1.2.3.4")',
    'ipaddress.IPv4Network("10.0.0.0/8")',
    'ipaddress.IPv6Network("2001:db8::/32")',
]
# the checker does not look at time zones, so the runtime rejects these where the checker stays quiet
# and `json.dumps` and the one-character rule of "char" are value checks the checker never makes
RUNTIME_ONLY: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("timestamptz", "datetime.datetime(2024, 1, 2, 3, 4, 5)"),
        ("timestamp", "datetime.datetime(2024, 1, 2, 3, 4, 5, tzinfo=datetime.UTC)"),
        *(("json", source) for source in NOT_JSON),
        *(("jsonb", source) for source in NOT_JSON),
        ('"char"', '"ok"'),
        ('"char"', f'"{UUID_TEXT}"'),
        ('"char"[]', '["ok"]'),
        # a `str` is accepted by type, but only a parseable address or MAC encodes
        *((decl, source) for decl in ("inet", "cidr", "macaddr", "macaddr8") for source in ('"ok"', f'"{UUID_TEXT}"')),
        *((f"{decl}[]", '["ok"]') for decl in ("inet", "cidr", "macaddr", "macaddr8")),
    },
)


async def run_params(dsn: str, cells: list[tuple[str, object]]) -> list[object]:
    outcomes: list[object] = []
    async with await connect(dsn) as conn:
        for decl, value in cells:
            template = Template("SELECT ", Interpolation(value, "value", None, ""), f"::{decl}")
            try:
                outcomes.append(await query_for(object, template).one(conn))
            except Error as error:
                outcomes.append(error)
    return outcomes


def test_param_matrix(schema: str) -> None:
    types = [sql for sql in column_types() if not sql.void]
    statements = {sql.decl: f"SELECT $1::{sql.decl}" for sql in types}
    outcomes = asyncio.run(describe_all(schema, list(statements.values()), 1, TIMEOUT_MS))
    namespace: dict[str, Any] = {}
    exec(PRELUDE, namespace)  # ruff: ignore[exec-builtin]
    evidence: dict[str, Any] = {}
    for source in PARAM_VALUES:
        tree = ast.parse(PRELUDE + source)
        evidence[source] = ParamInference(tree).infer(tree.body[-1].value)  # ty: ignore[unresolved-attribute]

    cells = [(sql, source) for sql in types for source in PARAM_VALUES]
    runtime = asyncio.run(run_params(schema, [(sql.decl, eval(source, namespace)) for sql, source in cells]))  # ruff: ignore[suspicious-eval-usage]
    problems: list[str] = []
    evident = 0
    for (sql, source), outcome in zip(cells, runtime, strict=True):
        described = outcomes[statements[sql.decl]]
        assert isinstance(described, Describe), f"{sql.decl}: {described}"
        (param,) = described.params
        found = compare_param(0, param.type_name, param.type_key, evidence[source], param.element_key)
        encode_error = isinstance(outcome, EncodeError)
        label = f"{sql.decl} <- {source}"
        if found is not None:
            evident += 1
            if not encode_error:
                problems.append(f"{label}: the checker rejects it but the runtime accepts it ({outcome!r})")
        elif encode_error and evidence[source] is not None and (sql.decl, source) not in RUNTIME_ONLY:
            problems.append(f"{label}: the runtime raises EncodeError but the checker accepts it")
        if (sql.decl, source) in RUNTIME_ONLY:
            assert found is None, label
            assert encode_error, label
    assert evident > len(types)
    assert not problems, "\n".join(problems)
