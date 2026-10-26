from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Final, Self

from rowx._rowplan import NO_ROWS, RowPlan, row_plan
from rowx._template import Compiled, compile_template
from rowx.errors import InterfaceError, RowNotFound

if TYPE_CHECKING:
    from collections.abc import Callable
    from string.templatelib import Template
    from types import TracebackType

    from rowx._executor import Executor, StreamHandle

DEFAULT_BATCH_SIZE: Final[int] = 1024


@dataclass(slots=True, kw_only=True, frozen=True)
class QueryResult:
    rows_affected: int


class RowStream[T]:
    def __init__(self, open_stream: Callable[[], StreamHandle], connection_closed: Callable[[], bool], /) -> None:
        self._open_stream = open_stream
        self._connection_closed = connection_closed
        self._impl: StreamHandle | None = None
        self._buffer: deque[object] = deque()
        self._done = False
        self.result: QueryResult | None = None

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> T:
        while True:
            # rows buffered from an earlier batch must not outlive the connection
            if not self._done and self._connection_closed():
                self._done = True
                self._buffer.clear()
                msg = "connection is closed"
                raise InterfaceError(msg)
            if self._buffer:
                return self._buffer.popleft()  # ty: ignore[invalid-return-type]
            if self._done:
                raise StopAsyncIteration
            # the connection is claimed by the first _next, so ConnectionBusyError and the
            # parameter and arity errors surface here and not from fetch()
            if self._impl is None:
                self._impl = self._open_stream()
            if batch := await self._impl._next():  # ruff: ignore[private-member-access]
                self._buffer.extend(batch)
                continue
            self._done = True
            if (rows_affected := self._impl.rows_affected) is not None:
                self.result = QueryResult(rows_affected=rows_affected)
            raise StopAsyncIteration

    async def aclose(self) -> None:
        self._done = True
        self._buffer.clear()
        if self._impl is not None:
            self._impl.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()


class _BaseQuery[T]:
    _dynamic: ClassVar[bool]

    def __init__(self, template: Template, /) -> None:
        self._compiled: Compiled
        self._params: list[object]
        self._compiled, self._params = compile_template(template, dynamic=self._dynamic)

    def _plan(self) -> RowPlan:
        # __orig_class__ is only set by typing after __init__, so read it lazily
        orig = getattr(self, "__orig_class__", None)
        return row_plan(orig.__args__[0]) if orig is not None else NO_ROWS

    def _row_plan(self) -> RowPlan:
        plan = self._plan()
        if plan.kind == "none":
            msg = "query without a row type; use .execute() or add query[T]"
            raise InterfaceError(msg)
        return plan

    async def _rows(self, conn: Executor, *, limit_one: bool) -> list[object]:
        plan = self._row_plan()
        return await conn._fetch(  # ruff: ignore[private-member-access]
            self._compiled.sql,
            self._params,
            plan.flags,
            plan.kind == "scalar",
            limit_one,
            plan.arity,
        )

    async def one(self, conn: Executor, /) -> T:
        rows = await self._rows(conn, limit_one=True)
        if not rows:
            msg = "query returned no rows"
            raise RowNotFound(msg)
        return rows[0]  # ty: ignore[invalid-return-type]

    async def optional(self, conn: Executor, /) -> T | None:
        rows = await self._rows(conn, limit_one=True)
        return rows[0] if rows else None  # ty: ignore[invalid-return-type]

    async def all(self, conn: Executor, /) -> list[T]:
        return await self._rows(conn, limit_one=False)  # ty: ignore[invalid-return-type]

    def fetch(self, conn: Executor, /, *, batch_size: int = DEFAULT_BATCH_SIZE) -> RowStream[T]:
        plan = self._row_plan()
        if batch_size < 1:
            msg = "batch_size must be at least 1"
            raise ValueError(msg)

        def open_stream() -> StreamHandle:
            return conn._stream(  # ruff: ignore[private-member-access]
                self._compiled.sql,
                self._params,
                plan.flags,
                plan.kind == "scalar",
                plan.arity,
                batch_size,
            )

        return RowStream(open_stream, lambda: conn.closed)

    async def execute(self, conn: Executor, /) -> QueryResult:
        rows_affected = await conn._execute(self._compiled.sql, self._params)  # ruff: ignore[private-member-access]
        return QueryResult(rows_affected=rows_affected)


class query[T = None](_BaseQuery[T]):  # ruff: ignore[invalid-class-name]
    _dynamic: ClassVar[bool] = False


class query_unchecked[T = None](_BaseQuery[T]):  # ruff: ignore[invalid-class-name]
    _dynamic: ClassVar[bool] = True
