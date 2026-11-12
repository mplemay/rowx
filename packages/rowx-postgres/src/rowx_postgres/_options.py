from dataclasses import dataclass, field, fields, replace
from typing import TYPE_CHECKING, Final, Literal, Self, override

from rowx_postgres._core import parse_dsn as parse_dsn_impl
from rowx_postgres._embedded import EmbeddedTarget, is_embedded_url, parse_embedded_url

if TYPE_CHECKING:
    from collections.abc import Mapping

DEFAULT_STATEMENT_CACHE_SIZE: Final[int] = 256


@dataclass(slots=True, kw_only=True, frozen=True, repr=False)
class ConnectOptions:
    host: str | None = None
    port: int | None = None
    user: str | None = None
    password: str | None = field(default=None, repr=False)
    database: str | None = None
    sslmode: str | None = None
    sslrootcert: str | None = None
    channel_binding: Literal["disable", "prefer", "require"] | None = None
    connect_timeout: float | None = None
    statement_cache_size: int = DEFAULT_STATEMENT_CACHE_SIZE
    application_name: str | None = None
    server_settings: Mapping[str, str] = field(default_factory=dict)
    extra: Mapping[str, str] = field(default_factory=dict)
    # set by an `embedded://` URL; `connect` and `Pool` start the instance and swap it for its socket (`with_socket`)
    embedded: EmbeddedTarget | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.embedded is None:
            return
        # replace() runs this again, so none of these can be overridden afterwards either
        for name in ("host", "port", "user", "password", "sslmode"):
            if getattr(self, name) is not None:
                msg = f"{name} cannot be set on an embedded URL"
                raise ValueError(msg)

    @classmethod
    def from_dsn(cls, dsn: str, /, **overrides: object) -> Self:
        if is_embedded_url(dsn):
            target, query = parse_embedded_url(dsn)
            # the other parameters are read as for a socket DSN, so `options=-c search_path=x` still works
            forwarded = parse_dsn_impl(f"postgresql:///?{query}")
            return replace(cls(**forwarded, embedded=target), **overrides)
        return replace(cls(**parse_dsn_impl(dsn)), **overrides)

    # the same options pointed at the instance's socket DSN; Rust never sees an embedded target
    def with_socket(self, dsn: str, /) -> Self:
        socket = parse_dsn_impl(dsn)
        return replace(
            self,
            embedded=None,
            host=socket["host"],
            port=socket["port"],
            user=socket["user"],
            sslmode=socket["sslmode"],
            database=self.database if self.database is not None else socket["database"],
        )

    def with_connect_timeout(self, seconds: float) -> Self:
        return replace(self, connect_timeout=seconds)

    # the dataclass repr, except that `extra` values (which can hold secrets such as `sslpassword`) are hidden
    @override
    def __repr__(self) -> str:
        # a non-mapping `extra` must not make the repr raise
        try:
            shown = {"extra": dict.fromkeys(self.extra, "<redacted>")}
        except TypeError:
            shown = {"extra": "<redacted>"}
        parts = [
            f"{item.name}={shown.get(item.name, getattr(self, item.name))!r}" for item in fields(self) if item.repr
        ]
        # only an embedded URL adds a part, so the repr of every other options object is what it always was
        if self.embedded is not None:
            parts.append(f"embedded={self.embedded!r}")
        return f"{type(self).__qualname__}({', '.join(parts)})"
