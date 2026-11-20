from dataclasses import dataclass, replace
from typing import Final, Literal, Self

from rowx_sqlite._core import parse_dsn as parse_dsn_impl

DEFAULT_STATEMENT_CACHE_SIZE: Final[int] = 256

# `ro` opens an existing file read-only, `rw` an existing file for writing and `rwc` also creates it
type OpenMode = Literal["ro", "rw", "rwc"]
type TransactionMode = Literal["deferred", "immediate", "exclusive"]


# `read_only` also sets `PRAGMA query_only` and wins over `mode`; `connect_timeout` is SQLite's busy timeout, the
# time a statement waits for a lock (5 seconds when unset); `statement_timeout` interrupts a statement that runs
# longer, and 0 or `None` leaves it unlimited
@dataclass(slots=True, kw_only=True, frozen=True)
class ConnectOptions:
    path: str | None = None
    memory: bool = False
    mode: OpenMode = "rw"
    connect_timeout: float | None = None
    statement_timeout: float | None = None
    statement_cache_size: int = DEFAULT_STATEMENT_CACHE_SIZE
    read_only: bool = False

    @classmethod
    def from_dsn(cls, dsn: str, /, **overrides: object) -> Self:
        return replace(cls(**parse_dsn_impl(dsn)), **overrides)

    def with_connect_timeout(self, seconds: float) -> Self:
        return replace(self, connect_timeout=seconds)
