import importlib
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol, Self, cast
from urllib.parse import urlparse

if TYPE_CHECKING:
    from datetime import datetime, timedelta
    from os import PathLike
    from types import TracebackType

type NullReason = Literal[
    "NotNullColumn",
    "NullableColumn",
    "NotValidatedConstraint",
    "ForeignOrView",
    "ViewColumn",
    "OuterJoin",
    "GroupingSets",
    "ScanBoundary",
    "ReturningOldNew",
    "SetOperation",
    "ComputedExpr",
    "Computed",
    "ExplainFailed",
    "OverrideBang",
    "OverrideQuestion",
]
type IssueKind = Literal[
    "missing-column",
    "unexpected-column",
    "duplicate-column",
    "ambiguous-column",
    "column-name-mismatch",
    "arity-mismatch",
]


class BackendMissing(Exception):  # ruff: ignore[error-suffix-on-exception-name]
    pass


class UnsupportedSchemeError(Exception):
    pass


class ShapeIssue(Protocol):
    @property
    def kind(self) -> IssueKind: ...
    @property
    def columns(self) -> Sequence[int]: ...
    @property
    def fields(self) -> Sequence[int]: ...
    @property
    def name(self) -> str | None: ...
    @property
    def min(self) -> int | None: ...
    @property
    def max(self) -> int | None: ...
    @property
    def actual(self) -> int | None: ...


class ColumnInfo(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def type_name(self) -> str: ...
    @property
    def type_key(self) -> str | None: ...
    @property
    def nullable(self) -> bool: ...
    @property
    def reason(self) -> NullReason: ...
    @property
    def reason_detail(self) -> str | None: ...
    @property
    def element_key(self) -> str | None: ...
    @property
    def raw_name(self) -> str: ...


class ParamInfo(Protocol):
    @property
    def type_name(self) -> str: ...
    @property
    def type_key(self) -> str | None: ...
    @property
    def element_key(self) -> str | None: ...


class Describe(Protocol):
    @property
    def params(self) -> Sequence[ParamInfo]: ...
    @property
    def columns(self) -> Sequence[ColumnInfo]: ...


class Connection(Protocol):
    async def close(self) -> None: ...


type FieldSpec = tuple[Sequence[str] | None, bool]
type ConnectChecker = Callable[[str, float], Awaitable[Connection]]
type DescribeQuery = Callable[[Connection, str], Awaitable[Describe]]
type ParseDsn = Callable[[str], dict[str, Any]]
type MapColumns = Callable[
    [Sequence[str], Sequence[FieldSpec], bool],
    tuple[Sequence[int | None], Sequence[ShapeIssue]],
]


class ConnectTarget(Protocol):
    @property
    def database(self) -> str | None: ...
    @property
    def connect_timeout(self) -> float | None: ...
    def with_connect_timeout(self, seconds: float) -> Self: ...


class DatabaseInfo(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def comment(self) -> str | None: ...
    @property
    def sessions(self) -> int: ...


class Migration(Protocol):
    @property
    def version(self) -> int: ...
    @property
    def description(self) -> str: ...
    @property
    def kind(self) -> str: ...
    @property
    def checksum(self) -> bytes: ...


class MigrationResult(Protocol):
    @property
    def migration(self) -> Migration: ...
    @property
    def elapsed(self) -> timedelta | None: ...


class MigrationStatus(Protocol):
    @property
    def version(self) -> int: ...
    @property
    def description(self) -> str: ...
    @property
    def state(self) -> str: ...
    @property
    def kind(self) -> str | None: ...
    @property
    def installed_on(self) -> datetime | None: ...
    @property
    def execution_time(self) -> timedelta | None: ...
    @property
    def local_checksum(self) -> bytes | None: ...
    @property
    def applied_checksum(self) -> bytes | None: ...


class DatabaseConnection(Protocol):
    async def close(self) -> None: ...
    async def __aenter__(self) -> Self: ...
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


class DatabasePool(Protocol):
    async def open(self) -> None: ...
    async def close(self) -> None: ...


class Migrator(Protocol):
    @property
    def migrations(self) -> Sequence[Migration]: ...
    # `Any` so the concrete migrator, which takes its own connection and pool, still satisfies this protocol
    async def run(
        self,
        executor: Any,  # ruff: ignore[any-type]
        /,
        *,
        target: int | None = None,
        dry_run: bool = False,
    ) -> Sequence[MigrationResult]: ...
    async def revert(
        self,
        executor: Any,  # ruff: ignore[any-type]
        /,
        *,
        target: int | None = None,
        all: bool = False,  # ruff: ignore[builtin-argument-shadowing]
        dry_run: bool = False,
    ) -> Sequence[MigrationResult]: ...
    async def skip(
        self,
        executor: Any,  # ruff: ignore[any-type]
        /,
        *,
        target: int | None = None,
        dry_run: bool = False,
    ) -> Sequence[MigrationResult]: ...
    async def info(
        self,
        executor: Any,  # ruff: ignore[any-type]
        /,
    ) -> Sequence[MigrationStatus]: ...


class DatabaseExists(Protocol):
    async def __call__(self, target: str | ConnectTarget, name: str | None = None) -> bool: ...


class CreateDatabase(Protocol):
    async def __call__(
        self,
        target: str | ConnectTarget,
        name: str | None = None,
        *,
        template: str | None = None,
        comment: str | None = None,
    ) -> None: ...


class DropDatabase(Protocol):
    async def __call__(
        self,
        target: str | ConnectTarget,
        name: str | None = None,
        *,
        force: bool = False,
    ) -> None: ...


class ListDatabases(Protocol):
    async def __call__(self, target: str | ConnectTarget, prefix: str) -> Sequence[DatabaseInfo]: ...


class WithDatabase(Protocol):
    def __call__(self, dsn: str, name: str) -> str: ...


class LoadMigrator(Protocol):
    def __call__(
        self,
        path: str | PathLike[str],
        /,
        *,
        table: str = ...,
        create_schemas: Iterable[str] = (),
        allow_out_of_order: bool = False,
        ignore_missing: bool = False,
    ) -> Migrator: ...


class Connect(Protocol):
    async def __call__(self, target: str | ConnectTarget, /) -> DatabaseConnection: ...


class OpenChecker(Protocol):
    async def __call__(self, dsn: str, timeout_s: float) -> DatabaseConnection: ...


class TargetOptions(Protocol):
    def __call__(self, dsn: str) -> ConnectTarget: ...


class TemplateMaterialize(Protocol):
    async def __call__(
        self,
        server_dsn: str,
        lock: DatabaseConnection,
        migrator: Migrator | None,
        name: str,
        /,
    ) -> None: ...


class TemplateBuild(Protocol):
    async def __call__(
        self,
        server_dsn: str,
        lock: DatabaseConnection,
        migrator: Migrator | None,
        name: str,
        materialize: TemplateMaterialize,
        /,
    ) -> None: ...


class EnsureTemplate(Protocol):
    async def __call__(  # ruff: ignore[too-many-arguments]
        self,
        server_dsn: str,
        migrator: Migrator | None,
        name: str,
        *,
        keep_stale: bool,
        lock_database: str,
        lock_fallback_states: frozenset[str],
        lock_key: int,
        template_key: Callable[[str], int],
        build_key: int,
        project: str,
        test_prefix: str,
        template_prefix: str,
        tmp_marker: str,
        base_template: str,
        build: TemplateBuild,
    ) -> DatabaseConnection: ...


class CloneDatabase(Protocol):
    async def __call__(
        self,
        server_dsn: str,
        template: str,
        project: str,
        *,
        test_prefix: str,
    ) -> str: ...


class PoolFactory(Protocol):
    def __call__(self, dsn: str) -> DatabasePool: ...


@dataclass(slots=True, kw_only=True, frozen=True)
class AdminBackend:
    database_exists: DatabaseExists
    create_database: CreateDatabase
    drop_database: DropDatabase
    list_databases: ListDatabases
    with_database: WithDatabase


@dataclass(slots=True, kw_only=True, frozen=True)
class MigrateBackend:
    load_migrator: LoadMigrator
    connect: Connect
    open_checker: OpenChecker
    options: TargetOptions


@dataclass(slots=True, kw_only=True, frozen=True)
class TestingBackend:
    ensure_template: EnsureTemplate
    clone: CloneDatabase
    pool: PoolFactory


@dataclass(slots=True, kw_only=True, frozen=True)
class TypeEntry:
    matcher: str
    py_result: str
    alternatives: frozenset[str]
    accepted_params: tuple[str, ...]


@dataclass(slots=True, kw_only=True, frozen=True)
class CheckerBackend:
    connect_checker: ConnectChecker
    describe: DescribeQuery
    parse_dsn: ParseDsn
    map_columns: MapColumns
    types: Mapping[str, TypeEntry]
    param_classes: Mapping[str, str]
    value_leaves: Mapping[str, str]
    range_leaves: Mapping[str, str]
    range_classes: frozenset[str]
    value_classes: Mapping[str, str]
    admin: AdminBackend
    migrate: MigrateBackend
    testing: TestingBackend


class _LoadedBackend(Protocol):
    def load(self) -> CheckerBackend: ...


# module path, then the extra name reported when that module is not installed
SCHEMES: Final[dict[str, tuple[str, str]]] = {
    "postgres": ("rowx.postgres._backend", "postgres"),
    "postgresql": ("rowx.postgres._backend", "postgres"),
}
DEFAULT_SCHEME: Final[str] = "postgres"
_LOADED: dict[str, CheckerBackend] = {}


def _parsed(token: str) -> str | None:
    # urlparse rejects some URLs (`postgresql://user@[broken`) before it yields a scheme; the scheme is still the
    # part before `://`, parsed on a placeholder so the result still comes from urllib.parse
    try:
        parsed = urlparse(token)
    except ValueError:
        head, sep, _rest = token.partition("://")
        if not sep or not head:
            return None
        try:
            parsed = urlparse(f"{head}://placeholder")
        except ValueError:
            return None
    if parsed.scheme and token.lower().startswith(f"{parsed.scheme}://"):
        return parsed.scheme
    return None


def _scheme(dsn: str) -> str | None:
    # the first token of a URL is `scheme://...`; urlparse lowercases it. A keyword DSN has no scheme.
    stripped = dsn.strip()
    if not stripped:
        return None
    return _parsed(stripped.split(None, 1)[0])


def _target(dsn: str) -> tuple[str, str]:
    scheme = _scheme(dsn)
    if scheme is None:
        scheme = DEFAULT_SCHEME
    if (target := SCHEMES.get(scheme)) is None:
        msg = f"unknown database scheme `{scheme}`"
        raise UnsupportedSchemeError(msg)
    return target


def _is_missing_backend(error: ModuleNotFoundError) -> bool:
    name = error.name
    return name is not None and (name == "rowx.postgres" or name.startswith("rowx.postgres."))


def _load(module_name: str, extra: str) -> CheckerBackend:
    if (loaded := _LOADED.get(module_name)) is not None:
        return loaded
    try:
        module = importlib.import_module(module_name)
        backend = cast("_LoadedBackend", module).load()
    except ModuleNotFoundError as error:
        if _is_missing_backend(error):
            msg = f"install `rowx[{extra}]`"
            raise BackendMissing(msg) from None
        raise
    _LOADED[module_name] = backend
    return backend


def module_for(dsn: str) -> str:
    return _target(dsn)[0]


def backend_for(dsn: str) -> CheckerBackend:
    module_name, extra = _target(dsn)
    return _load(module_name, extra)


def default_backend() -> CheckerBackend:
    module_name, extra = SCHEMES[DEFAULT_SCHEME]
    return _load(module_name, extra)
