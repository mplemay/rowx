import asyncio
import sys
import types
from dataclasses import dataclass, field
from importlib import import_module
from string.templatelib import Template
from typing import TYPE_CHECKING, Final, cast

import pytest

import rowx.postgres._core as core_postgres
from rowx import query
from rowx.__tests__.conformance.row_types import (
    IMPORTED_TYPE_VARIABLE_CASES,
    NAMED_ROWS,
    PRELUDE,
    ROWS,
    TYPE_NAME_ROWS,
    NamedPlan,
    Plan,
)
from rowx._rowplan import row_plan
from rowx.errors import InterfaceError
from rowx.postgres import Connection, connect
from rowx.postgres._core import RowShape

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

type Evaluate = Callable[[str], object]


@dataclass(slots=True)
class FakeImpl:
    fetches: list[tuple[list[int] | None, bool, int | None, object]] = field(default_factory=list)

    async def _fetch(
        self,
        _sql: str,
        _params: list[object],
        mask: list[int] | None,
        scalar: bool,  # ruff: ignore[boolean-type-hint-positional-argument]
        _limit_one: bool,  # ruff: ignore[boolean-type-hint-positional-argument]
        arity: int | None,
        shape: object,
    ) -> list[object]:
        self.fetches.append((mask, scalar, arity, shape))
        return []


@pytest.fixture
def evaluate(monkeypatch: pytest.MonkeyPatch) -> Evaluate:
    def run(source: str) -> object:
        # a class resolves its annotations in its module, so the source runs in a registered one that goes with the test
        module = types.ModuleType("conformance_row")
        monkeypatch.setitem(sys.modules, "conformance_row", module)
        exec(PRELUDE + source, module.__dict__)  # ruff: ignore[exec-builtin]
        return module.T

    return run


def fetch_plan(t: object) -> Plan | NamedPlan:
    impl = FakeImpl()
    conn = Connection(impl)  # ty: ignore[invalid-argument-type]
    asyncio.run(query[t](t"SELECT 1").all(conn))  # ty: ignore[invalid-type-form]
    ((mask, scalar, arity, shape),) = impl.fetches
    if shape is not None:
        # a named plan reaches the driver as its shape, with no mask or arity
        assert mask is None
        assert arity is None
        assert not scalar
        assert isinstance(shape, RowShape)
        fields = tuple((f.key, f.names, f.nullable, f.required) for f in row_plan(t).fields)
        return ("named", shape.form, shape.matching, fields)
    assert mask is not None
    assert arity is not None
    # bit 0 of each flag is the nullability the table lists
    return ("scalar" if scalar else "tuple", arity, tuple(bool(flag & 1) for flag in mask))


@pytest.mark.parametrize(("source", "expected", "_verdict"), ROWS)
def test_conformance_row_at_first_fetch(
    source: str,
    expected: Plan | NamedPlan | str,
    _verdict: str,
    evaluate: Evaluate,
) -> None:
    t = evaluate(source)
    if isinstance(expected, str):
        with pytest.raises(InterfaceError):
            fetch_plan(t)
    elif expected[0] == "none":
        with pytest.raises(InterfaceError, match="without a row type"):
            fetch_plan(t)
    else:
        assert fetch_plan(t) == expected


def test_interval_leaf_reaches_the_driver_as_flag_bit_one(evaluate: Evaluate) -> None:
    impl = FakeImpl()
    conn = Connection(impl)  # ty: ignore[invalid-argument-type]
    row = evaluate("T = tuple[Interval, datetime.timedelta, Interval | None]")
    asyncio.run(query[row](t"SELECT 1").all(conn))  # ty: ignore[invalid-type-form]
    ((mask, _, _, _),) = impl.fetches
    assert mask == [0b10, 0b00, 0b11]


type ShapeField = tuple[str, tuple[str, ...] | None, bool, int]


def build_shape_arguments(monkeypatch: pytest.MonkeyPatch, t: object) -> tuple[str, str, list[ShapeField]]:
    captured: list[tuple[str, str, list[ShapeField]]] = []
    original = core_postgres.RowShape

    def spy(*args: object, validate: bool = True) -> RowShape:
        _, form, matching, _, fields = args
        captured.append((cast("str", form), cast("str", matching), cast("list[ShapeField]", fields)))
        return original(*args, validate=validate)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(core_postgres, "RowShape", spy)
    row_plan(t)
    (args,) = captured
    return args


NAMED_PLAN_ROWS: Final[list[tuple[str, NamedPlan]]] = [
    (source, expected) for source, expected, _ in NAMED_ROWS if not isinstance(expected, str) and len(expected) == 4
]


@pytest.mark.parametrize(("source", "expected"), NAMED_PLAN_ROWS)
def test_the_shape_built_for_a_named_row_carries_the_required_and_nullable_flags(
    monkeypatch: pytest.MonkeyPatch, source: str, expected: NamedPlan, evaluate: Evaluate
) -> None:
    _, form, matching, fields = expected
    shape_form, shape_matching, shape_fields = build_shape_arguments(monkeypatch, evaluate(source))
    assert (shape_form, shape_matching) == (form, matching)
    # flag bit 0 is nullable; bit 1 (the interval leaf) has its own test
    assert [(key, names, required, flag & 1) for key, names, required, flag in shape_fields] == [
        (key, names, required, int(nullable)) for key, names, nullable, required in fields
    ]


def test_the_shape_built_for_interval_fields_carries_flag_bit_one(
    monkeypatch: pytest.MonkeyPatch, evaluate: Evaluate
) -> None:
    source = "@dataclass\nclass T:\n    i: Interval\n    j: Interval | None\n    d: datetime.timedelta\n    k: list[Interval]"
    *_, shape_fields = build_shape_arguments(monkeypatch, evaluate(source))
    assert [flag for *_, flag in shape_fields] == [0b10, 0b11, 0b00, 0b10]


DB_ROWS: Final[list[tuple[str, str]]] = [
    ("T = int", "SELECT 1"),
    ("T = tuple[int, str | None]", "SELECT 1, NULL::text"),
    ("T = list[int | None]", "SELECT ARRAY[1, NULL]::int[]"),
]
DB_EXPECTED: Final[list[object]] = [1, (1, None), [1, None]]


@pytest.mark.integration
@pytest.mark.db
@pytest.mark.parametrize(("row", "expected"), list(zip(DB_ROWS, DB_EXPECTED, strict=True)), ids=[r[0] for r in DB_ROWS])
def test_conformance_ok_row_against_db(schema: str, row: tuple[str, str], expected: object, evaluate: Evaluate) -> None:
    source, sql = row
    t = evaluate(source)

    async def body() -> object:
        async with await connect(schema) as conn:
            return await query[t](Template(sql)).one(conn)  # ty: ignore[invalid-type-form]

    assert asyncio.run(body()) == expected


@pytest.mark.parametrize(("source", "type_name"), TYPE_NAME_ROWS)
def test_the_type_name_of_a_generic_row_is_the_canonical_text(source: str, type_name: str, evaluate: Evaluate) -> None:
    assert row_plan(evaluate(source)).type_name == type_name


@pytest.mark.parametrize(("name", "files", "expected"), IMPORTED_TYPE_VARIABLE_CASES)
def test_type_variables_imported_from_other_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, files: dict[str, str], expected: NamedPlan | str
) -> None:
    for filename, source in files.items():
        (tmp_path / filename).write_text(source)
    monkeypatch.syspath_prepend(str(tmp_path))
    for filename in files:
        monkeypatch.delitem(sys.modules, filename.removesuffix(".py"), raising=False)
    try:
        t = import_module("main").T
        if isinstance(expected, str):
            with pytest.raises(InterfaceError):
                fetch_plan(t)
        else:
            assert fetch_plan(t) == expected, name
    finally:
        for filename in files:
            sys.modules.pop(filename.removesuffix(".py"), None)
