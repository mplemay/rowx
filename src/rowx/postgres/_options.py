from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Final, Self

from rowx._core.postgres import parse_dsn as parse_dsn_impl

if TYPE_CHECKING:
    from collections.abc import Mapping

DEFAULT_STATEMENT_CACHE_SIZE: Final[int] = 256


@dataclass(slots=True, kw_only=True, frozen=True)
class ConnectOptions:
    host: str | None = None
    port: int | None = None
    user: str | None = None
    password: str | None = field(default=None, repr=False)
    database: str | None = None
    sslmode: str | None = None
    sslrootcert: str | None = None
    connect_timeout: float | None = None
    statement_cache_size: int = DEFAULT_STATEMENT_CACHE_SIZE
    application_name: str | None = None
    server_settings: Mapping[str, str] = field(default_factory=dict)
    extra: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_dsn(cls, dsn: str, /, **overrides: object) -> Self:
        return replace(cls(**parse_dsn_impl(dsn)), **overrides)
