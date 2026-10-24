from typing import TYPE_CHECKING, ClassVar, Self

from rowx._rowplan import NO_ROWS, RowPlan, row_plan
from rowx._template import Compiled, compile_template
from rowx.errors import InterfaceError, RowNotFound

if TYPE_CHECKING:
    from string.templatelib import Template
    from types import TracebackType

    from rowx._core import Connection as ConnectionImpl


class Connection:
    def __init__(self, impl: ConnectionImpl, /) -> None:
        self._impl = impl

    async def close(self) -> None:
        await self._impl.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    @property
    def closed(self) -> bool:
        return self._impl.closed


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

    async def _rows(self, conn: Connection, *, limit_one: bool) -> list[object]:
        plan = self._plan()
        if plan.kind == "none":
            msg = "query without a row type; use .execute() or add query[T]"
            raise InterfaceError(msg)
        return await conn._impl._fetch(  # noqa: SLF001
            self._compiled.sql,
            self._params,
            list(plan.nullable_mask),
            plan.kind == "scalar",
            limit_one,
            plan.arity,
        )

    async def fetch_one(self, conn: Connection, /) -> T:
        rows = await self._rows(conn, limit_one=True)
        if not rows:
            msg = "query returned no rows"
            raise RowNotFound(msg)
        return rows[0]  # ty: ignore[invalid-return-type]

    async def fetch_all(self, conn: Connection, /) -> list[T]:
        return await self._rows(conn, limit_one=False)  # ty: ignore[invalid-return-type]

    async def execute(self, conn: Connection, /) -> int:
        return await conn._impl._execute(self._compiled.sql, self._params)  # noqa: SLF001


class query[T = None](_BaseQuery[T]):  # noqa: N801
    _dynamic: ClassVar[bool] = False


class query_unchecked[T = None](_BaseQuery[T]):  # noqa: N801
    _dynamic: ClassVar[bool] = True
