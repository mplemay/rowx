from dataclasses import dataclass, field
from typing import TYPE_CHECKING, override

import pytest
from rowx_core import Compiler, Executor, InterfaceError, query, query_unchecked
from rowx_core._template import Compiled, Parameter, TemplatePlan, iter_parts

if TYPE_CHECKING:
    from collections.abc import Sequence

    from rowx_core import ShapeSpec


@dataclass(slots=True, kw_only=True, frozen=True)
class FakeCompiler:
    marker: str

    def compile(self, plan: TemplatePlan, /) -> tuple[Compiled, list[object]]:
        text = ""
        params: list[object] = []
        for part in iter_parts(plan):
            if isinstance(part, str):
                text += part
            elif isinstance(part, Parameter):
                params.append(part.value)
                text += f"{self.marker}{len(params)}"
            else:
                text += ".".join(part.parts)
        return Compiled(text, (), (), ()), params

    def compile_parts(self, strings: Sequence[str], /) -> Compiled:
        return Compiled(self.marker.join(strings), (), (), ())


@dataclass(slots=True, kw_only=True)
class FakeStream:
    rows: list[object]
    rows_affected: int | None = 1

    async def _next(self) -> list[object]:
        rows, self.rows = self.rows, []
        return rows

    def close(self) -> None:
        self.rows = []


@dataclass(slots=True, kw_only=True)
class FakeExecutor(Executor):
    dialect: FakeCompiler
    rows: list[object] = field(default_factory=list)
    calls: list[tuple[str, list[object]]] = field(default_factory=list)
    closed: bool = False

    @property
    @override
    def compiler(self) -> Compiler:
        return self.dialect

    @override
    async def _fetch(
        self,
        sql: str,
        params: list[object],
        mask: Sequence[int] | None,
        scalar: bool,
        limit_one: bool,
        arity: int | None,
        shape: ShapeSpec | None,
    ) -> list[object]:
        self.calls.append((sql, params))
        return self.rows[:1] if limit_one else self.rows

    @override
    def _stream(
        self,
        sql: str,
        params: list[object],
        mask: Sequence[int] | None,
        scalar: bool,
        arity: int | None,
        batch_size: int,
        shape: ShapeSpec | None,
    ) -> FakeStream:
        self.calls.append((sql, params))
        return FakeStream(rows=list(self.rows))

    @override
    async def _execute(self, sql: str, params: list[object]) -> int:
        self.calls.append((sql, params))
        return len(params)


@pytest.mark.asyncio
async def test_query_compiles_for_each_executor() -> None:
    value = 7
    statement = query[int](t"SELECT {value}")
    first = FakeExecutor(dialect=FakeCompiler(marker="?"), rows=[value])
    second = FakeExecutor(dialect=FakeCompiler(marker="@"), rows=[value])

    assert await statement.one(first) == value
    assert await statement.all(second) == [value]
    assert await statement.optional(first) == value
    assert first.calls == [("SELECT ?1", [value]), ("SELECT ?1", [value])]
    assert second.calls == [("SELECT @1", [value])]


@pytest.mark.asyncio
async def test_execute_and_stream_use_the_executor_compiler() -> None:
    value = 8
    executor = FakeExecutor(dialect=FakeCompiler(marker="?"), rows=[value])
    result = await query(t"INSERT {value}").execute(executor)
    assert result.rows_affected == 1
    stream = query[int](t"SELECT {value}").fetch(executor)
    assert executor.calls == [("INSERT ?1", [value])]
    assert [row async for row in stream] == [value]
    assert executor.calls[-1] == ("SELECT ?1", [value])
    assert stream.result is not None
    assert stream.result.rows_affected == 1


@pytest.mark.asyncio
async def test_stream_discards_buffered_rows_when_executor_closes() -> None:
    executor = FakeExecutor(dialect=FakeCompiler(marker="?"), rows=[1, 2])
    stream = query[int](t"SELECT 1").fetch(executor)
    assert await anext(stream) == 1
    executor.closed = True
    with pytest.raises(InterfaceError, match="connection is closed"):
        await anext(stream)


def test_query_validates_without_selecting_a_compiler() -> None:
    value = 1
    with pytest.raises(TypeError, match="conversion !r is not supported"):
        query(t"SELECT {value!r}")
    with pytest.raises(TypeError, match="nested templates are only supported"):
        query(t"SELECT {t'{value}'}")
    with pytest.raises(TypeError, match="NUL character"):
        query(t"SELECT \x00")


def test_unchecked_capture_retains_shared_nested_templates() -> None:
    value = 1
    template = t"{value}"
    for _ in range(30):
        template = t"{template}{template}"
    statement = query_unchecked(template)
    assert statement is not None
