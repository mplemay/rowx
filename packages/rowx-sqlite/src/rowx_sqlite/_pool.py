from enum import Enum
from typing import TYPE_CHECKING, Final, Self, override

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

from rowx_sqlite._compiler import COMPILER
from rowx_sqlite._connection import Connection
from rowx_sqlite._core import Connection as ConnectionImpl, Pool as PoolImpl, RowShape
from rowx_sqlite._options import ConnectOptions
from rowx_sqlite._shape import native_shape
from rowx_sqlite._transaction import Transaction

if TYPE_CHECKING:
    from rowx_core import Compiler, ShapeSpec

    from rowx_sqlite._options import TransactionMode

type Hook = BaseHook[Connection]
type StreamArgs = BaseStreamArgs[RowShape]


# "the caller gave no value", which is not the same as `None` (no limit): the default depends on the database
class Unset(Enum):
    UNSET = "unset"


UNSET: Final = Unset.UNSET

# A `:memory:` database lives and dies with its one connection, so the pool keeps exactly that one, created at
# open, and never retires it
MEMORY_MIN_SIZE: Final[int] = 1
MEMORY_MAX_SIZE: Final[int] = 1


def resolve[T](name: str, value: T | Unset, default: T, memory: bool, *, kept: T) -> T:  # ruff: ignore[boolean-type-hint-positional-argument]
    if isinstance(value, Unset):
        return kept if memory else default
    if memory and value != kept:
        msg = f"{name} must be {kept!r} for a sqlite::memory: pool, whose one connection holds the database, not {value!r}"
        raise ValueError(msg)
    return value


class Acquire(BaseAcquire[Connection]):
    pass


class PoolTransaction(BasePoolTransaction[Connection, Transaction]):
    def __init__(self, pool: Pool, /, *, mode: TransactionMode | None, read_only: bool) -> None:
        super().__init__(pool, lambda conn: conn.transaction(mode=mode, read_only=read_only))


class PooledStream(BasePooledStream[Connection]):
    pass


# One pool, and every connection reads and writes: writes are serialised by SQLite, which a transaction takes the
# write lock for up front (`BEGIN IMMEDIATE`) and waits for up to the busy timeout.
# For `:memory:` the options left unset resolve to one eager connection with no idle timeout, lifetime or query cap,
# and an explicit value that says otherwise is refused. A lost connection breaks the pool for good.
class Pool(BasePool[Connection, ConnectionImpl, RowShape, PoolImpl, ConnectOptions]):
    @property
    def compiler(self) -> Compiler:
        return COMPILER

    def __init__(  # ruff: ignore[too-many-arguments]
        self,
        options: str | ConnectOptions,
        /,
        *,
        min_size: int | Unset = UNSET,
        max_size: int | Unset = UNSET,
        acquire_timeout: float = DEFAULT_ACQUIRE_TIMEOUT,
        idle_timeout: float | Unset | None = UNSET,
        max_lifetime: float | Unset | None = UNSET,
        max_queries: int | Unset | None = UNSET,
        ping_after: float | None = DEFAULT_PING_AFTER,
        init: Hook | None = None,
        setup: Hook | None = None,
        reset: Hook | None = None,
    ) -> None:
        resolved = ConnectOptions.from_dsn(options) if isinstance(options, str) else options
        memory = resolved.memory
        sizes = (
            resolve("min_size", min_size, 0, memory, kept=MEMORY_MIN_SIZE),
            resolve("max_size", max_size, DEFAULT_MAX_SIZE, memory, kept=MEMORY_MAX_SIZE),
        )
        idle = resolve("idle_timeout", idle_timeout, DEFAULT_IDLE_TIMEOUT, memory, kept=None)
        lifetime = resolve("max_lifetime", max_lifetime, DEFAULT_MAX_LIFETIME, memory, kept=None)
        queries = resolve("max_queries", max_queries, None, memory, kept=None)
        validate(
            min_size=sizes[0],
            max_size=sizes[1],
            acquire_timeout=acquire_timeout,
            idle_timeout=idle,
            max_lifetime=lifetime,
            max_queries=queries,
            ping_after=ping_after,
        )

        def build(target: ConnectOptions) -> PoolImpl:
            return PoolImpl(
                target,
                min_size=sizes[0],
                max_size=sizes[1],
                acquire_timeout=acquire_timeout,
                idle_timeout=idle,
                max_lifetime=lifetime,
                max_queries=queries,
                ping_after=ping_after,
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
        # ends an open transaction, so the hook sees an idle connection; the pool's own reset still runs, and it
        # only asks SQLite whether a transaction is open
        await impl.reset()
        return False

    @override
    def _pooled_stream(self, args: StreamArgs, /) -> PooledStream:
        return PooledStream(self, args)

    @override
    def acquire(self, *, timeout: float | None = None) -> Acquire:
        return Acquire(self, timeout)

    def transaction(self, *, mode: TransactionMode | None = None, read_only: bool = False) -> PoolTransaction:
        return PoolTransaction(self, mode=mode, read_only=read_only)
