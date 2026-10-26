from typing import TYPE_CHECKING, Self

from rowx.errors import ConnectionBusyError, InterfaceError

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType

    from rowx._core.postgres import RowStream as RowStreamImpl
    from rowx._executor import StreamHandle
    from rowx.postgres._connection import Connection


class TrackedStream:
    def __init__(self, inner: RowStreamImpl, on_failure: Callable[[], None], /) -> None:
        self._inner = inner
        self._on_failure = on_failure

    async def _next(self) -> list[object]:
        try:
            return await self._inner._next()  # ruff: ignore[private-member-access]
        except ConnectionBusyError:
            # nothing ran, so the transaction is untouched
            raise
        except BaseException:
            self._on_failure()
            raise

    def close(self) -> None:
        self._inner.close()

    @property
    def rows_affected(self) -> int | None:
        return self._inner.rows_affected


class Transaction:
    def __init__(
        self,
        conn: Connection,
        /,
        *,
        isolation: str | None = None,
        readonly: bool = False,
        deferrable: bool = False,
        parent: Self | None = None,
    ) -> None:
        self._conn = conn
        self._isolation = isolation
        self._readonly = readonly
        self._deferrable = deferrable
        self._parent = parent
        self._child: Self | None = None
        # the savepoint number; None for the outermost transaction
        self._savepoint: int | None = None
        self._active = False
        self._ended = False
        self._failed = False

    def _check_active(self) -> None:
        if self._ended:
            msg = "the transaction has ended"
            raise InterfaceError(msg)
        if not self._active:
            msg = "the transaction has not started; use it with `async with`"
            raise InterfaceError(msg)

    async def _fetch(  # ruff: ignore[too-many-arguments, too-many-positional-arguments]
        self,
        sql: str,
        params: list[object],
        mask: list[bool] | None,
        scalar: bool,  # ruff: ignore[boolean-type-hint-positional-argument]
        limit_one: bool,  # ruff: ignore[boolean-type-hint-positional-argument]
        arity: int | None,
    ) -> list[object]:
        self._check_active()
        # the connection records failures, so queries run on it directly are tracked as well
        return await self._conn._fetch(sql, params, mask, scalar, limit_one, arity)  # ruff: ignore[private-member-access]

    def _stream(  # ruff: ignore[too-many-arguments, too-many-positional-arguments]
        self,
        sql: str,
        params: list[object],
        mask: list[bool] | None,
        scalar: bool,  # ruff: ignore[boolean-type-hint-positional-argument]
        arity: int | None,
        batch_size: int,
    ) -> StreamHandle:
        self._check_active()
        return self._conn._stream(sql, params, mask, scalar, arity, batch_size)  # ruff: ignore[private-member-access]

    async def _execute(self, sql: str, params: list[object]) -> int:
        self._check_active()
        return await self._conn._execute(sql, params)  # ruff: ignore[private-member-access]

    @property
    def closed(self) -> bool:
        return not self._active or self._ended or self._conn.closed

    def transaction(
        self,
        *,
        isolation: str | None = None,
        readonly: bool = False,
        deferrable: bool = False,
    ) -> Transaction:
        if isolation is not None or readonly or deferrable:
            msg = "a savepoint takes no isolation, readonly or deferrable arguments"
            raise InterfaceError(msg)
        self._check_active()
        return Transaction(self._conn, parent=self)

    async def __aenter__(self) -> Self:
        if self._active or self._ended:
            msg = "a transaction can only be entered once"
            raise InterfaceError(msg)
        impl = self._conn._impl  # ruff: ignore[private-member-access]
        if self._parent is None:
            await impl._begin(self._isolation, self._readonly, self._deferrable)  # ruff: ignore[private-member-access]
        else:
            parent = self._parent
            parent._check_active()  # ruff: ignore[private-member-access]
            if parent._child is not None:  # ruff: ignore[private-member-access]
                msg = "a savepoint is already open in this transaction"
                raise InterfaceError(msg)
            self._savepoint = await impl._savepoint()  # ruff: ignore[private-member-access]
            parent._child = self  # ruff: ignore[private-member-access]
        self._active = True
        # the innermost open transaction is the one a failing statement aborts
        self._conn._tx = self  # ruff: ignore[private-member-access]
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._ended:
            return
        if exc_type is not None:
            await self._end(commit=False)
            return
        if self._child is not None:
            await self._end(commit=False)
            msg = "the transaction ended while a savepoint was still open"
            raise InterfaceError(msg)
        if self._failed:
            await self._end(commit=False)
            msg = "an error inside the transaction was swallowed; it was rolled back instead of committed"
            raise InterfaceError(msg)
        await self._end(commit=True)

    async def commit(self) -> None:
        self._check_active()
        self._check_no_child()
        if self._failed:
            await self._end(commit=False)
            msg = "an error inside the transaction was swallowed; it was rolled back instead of committed"
            raise InterfaceError(msg)
        await self._end(commit=True)

    async def rollback(self) -> None:
        self._check_active()
        self._check_no_child()
        await self._end(commit=False)

    def _check_no_child(self) -> None:
        if self._child is not None:
            msg = "end the open savepoint first"
            raise InterfaceError(msg)

    def _finish(self) -> None:
        # marked first, so nothing can reuse the transaction while the ending statement runs
        self._ended = True
        self._conn._tx = self._parent  # ruff: ignore[private-member-access]
        if self._parent is not None:
            self._parent._child = None  # ruff: ignore[private-member-access]

    async def _end(self, *, commit: bool) -> None:
        impl = self._conn._impl  # ruff: ignore[private-member-access]
        # each call claims the connection before it returns, so a busy connection raises here and the
        # transaction stays open; a cancelled statement that is still settling is waited for
        if self._savepoint is None:
            pending = impl._end_transaction(commit)  # ruff: ignore[private-member-access]
            self._finish()
            await pending
            return
        first = (
            impl._release_savepoint(self._savepoint)  # ruff: ignore[private-member-access]
            if commit
            else impl._rollback_to_savepoint(self._savepoint)  # ruff: ignore[private-member-access]
        )
        self._finish()
        await first
        if not commit:
            await impl._release_savepoint(self._savepoint)  # ruff: ignore[private-member-access]
