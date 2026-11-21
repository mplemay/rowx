from typing import TYPE_CHECKING, Self, override

from rowx_core._pool import (  # ruff: ignore[import-private-name]
    DEFAULT_ACQUIRE_TIMEOUT,
    DEFAULT_IDLE_TIMEOUT,
    DEFAULT_MAX_LIFETIME,
    DEFAULT_MAX_SIZE,
    DEFAULT_PING_AFTER,
    BaseAcquire,
    BasePool,
    BasePooledStream,
    BasePoolTransaction,
    Hook as BaseHook,
    StreamArgs as BaseStreamArgs,
    validate,
)

from rowx_postgres._compiler import COMPILER
from rowx_postgres._connection import RESET_QUERY, Connection
from rowx_postgres._core import Connection as ConnectionImpl, Pool as PoolImpl, RowShape
from rowx_postgres._options import ConnectOptions
from rowx_postgres._shape import native_shape
from rowx_postgres._transaction import Transaction

if TYPE_CHECKING:
    from rowx_core import Compiler, ShapeSpec

type Hook = BaseHook[Connection]
type StreamArgs = BaseStreamArgs[RowShape]


class Acquire(BaseAcquire[Connection]):
    pass


class PoolTransaction(BasePoolTransaction[Connection, Transaction]):
    def __init__(self, pool: Pool, /, *, isolation: str | None, readonly: bool, deferrable: bool) -> None:
        super().__init__(
            pool,
            lambda conn: conn.transaction(isolation=isolation, readonly=readonly, deferrable=deferrable),
        )


class PooledStream(BasePooledStream[Connection]):
    pass


class Pool(BasePool[Connection, ConnectionImpl, RowShape, PoolImpl, ConnectOptions]):
    @property
    def compiler(self) -> Compiler:
        return COMPILER

    def __init__(  # ruff: ignore[too-many-arguments]
        self,
        options: str | ConnectOptions,
        /,
        *,
        min_size: int = 0,
        max_size: int = DEFAULT_MAX_SIZE,
        acquire_timeout: float = DEFAULT_ACQUIRE_TIMEOUT,
        idle_timeout: float | None = DEFAULT_IDLE_TIMEOUT,
        max_lifetime: float | None = DEFAULT_MAX_LIFETIME,
        max_queries: int | None = None,
        ping_after: float | None = DEFAULT_PING_AFTER,
        init: Hook | None = None,
        setup: Hook | None = None,
        reset: Hook | None = None,
        reset_query: str | None = RESET_QUERY,
    ) -> None:
        validate(
            min_size=min_size,
            max_size=max_size,
            acquire_timeout=acquire_timeout,
            idle_timeout=idle_timeout,
            max_lifetime=max_lifetime,
            max_queries=max_queries,
            ping_after=ping_after,
        )
        resolved = ConnectOptions.from_dsn(options) if isinstance(options, str) else options
        self._reset_query = reset_query

        def build(target: ConnectOptions) -> PoolImpl:
            return PoolImpl(
                target,
                min_size=min_size,
                max_size=max_size,
                acquire_timeout=acquire_timeout,
                idle_timeout=idle_timeout,
                max_lifetime=max_lifetime,
                max_queries=max_queries,
                ping_after=ping_after,
                reset_query=reset_query,
            )

        super().__init__(resolved, build, acquire_timeout=acquire_timeout, init=init, setup=setup, reset=reset)

    @override
    def _wrap(self, impl: ConnectionImpl, /, *, pool: Self | None) -> Connection:
        return Connection(impl, pool=pool)

    @override
    def _native_shape(self, shape: ShapeSpec | RowShape | None, /) -> RowShape | None:
        return native_shape(shape)

    @override
    async def _reset_native(self, impl: ConnectionImpl, /) -> bool:
        # with no reset query this still ends an open transaction, so the hook sees an idle connection
        await impl.reset(self._reset_query)
        return self._reset_query is not None

    @override
    def _pooled_stream(self, args: StreamArgs, /) -> PooledStream:
        return PooledStream(self, args)

    @override
    def acquire(self, *, timeout: float | None = None) -> Acquire:
        return Acquire(self, timeout)

    def transaction(
        self,
        *,
        isolation: str | None = None,
        readonly: bool = False,
        deferrable: bool = False,
    ) -> PoolTransaction:
        return PoolTransaction(self, isolation=isolation, readonly=readonly, deferrable=deferrable)
