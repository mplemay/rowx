from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol, Self

from rowx_core._executor import Connection, Pool

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager
    from datetime import datetime, timedelta
    from os import PathLike
    from pathlib import Path
    from types import TracebackType

    from rowx_core._template import Compiler


type NullReason = Literal[
    "NotNullColumn",
    "NullableColumn",
    "ScanFilterNotNull",
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


type DatabaseConnection = Connection
type DatabasePool = Pool[Connection]


class Migrator[E](Protocol):
    @property
    def migrations(self) -> Sequence[Migration]: ...
    async def run(
        self, executor: E, /, *, target: int | None = None, dry_run: bool = False
    ) -> Sequence[MigrationResult]: ...
    async def revert(
        self,
        executor: E,
        /,
        *,
        target: int | None = None,
        all: bool = False,  # ruff: ignore[builtin-argument-shadowing]
        dry_run: bool = False,
    ) -> Sequence[MigrationResult]: ...
    async def skip(
        self, executor: E, /, *, target: int | None = None, dry_run: bool = False
    ) -> Sequence[MigrationResult]: ...
    async def info(self, executor: E, /) -> Sequence[MigrationStatus]: ...


class MigrationSession(Protocol):
    async def run(self, *, target: int | None = None, dry_run: bool = False) -> Sequence[MigrationResult]: ...
    async def revert(
        self,
        *,
        target: int | None = None,
        all: bool = False,  # ruff: ignore[builtin-argument-shadowing]
        dry_run: bool = False,
    ) -> Sequence[MigrationResult]: ...
    async def skip(self, *, target: int | None = None, dry_run: bool = False) -> Sequence[MigrationResult]: ...
    async def info(self) -> Sequence[MigrationStatus]: ...
    async def close(self) -> None: ...
    async def __aenter__(self) -> Self: ...
    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None: ...


class MigrationService(Protocol):
    @property
    def migrations(self) -> Sequence[Migration]: ...
    async def open(
        self, *, connect_timeout: float = 10.0, readonly: bool = False, statement_timeout: float = 10.0
    ) -> MigrationSession: ...
    async def run_on(self, dsn: str) -> Sequence[MigrationResult]: ...


type Failure = Literal["lost", "timeout", "operational", "other"]
type FieldSpec = tuple[Sequence[str] | None, bool]
type MapColumns = Callable[
    [Sequence[str], Sequence[FieldSpec], bool], tuple[Sequence[int | None], Sequence[ShapeIssue]]
]


class CheckerSession(Protocol):
    async def describe(self, sql: str) -> Describe: ...
    async def close(self) -> None: ...
    def classify(self, error: Exception) -> Failure: ...


@dataclass(slots=True, kw_only=True, frozen=True)
class TypeEntry:
    matcher: str
    py_result: str
    alternatives: frozenset[str]
    accepted_params: tuple[str, ...]


@dataclass(slots=True, kw_only=True, frozen=True)
class OverrideIssue:
    message: str
    offset: int
    help: str | None = None


@dataclass(slots=True, kw_only=True, frozen=True)
class CheckerBackend:
    connect_checker: Callable[[str, float], Awaitable[CheckerSession]]
    compiler: Compiler
    map_columns: MapColumns
    types: Mapping[str, TypeEntry]
    param_classes: Mapping[str, str]
    value_leaves: Mapping[str, str]
    range_leaves: Mapping[str, str]
    range_classes: frozenset[str]
    value_classes: Mapping[str, str]
    sql_error_help: Mapping[str, str] = field(default_factory=dict)
    override_issues: Callable[[str, Sequence[ColumnInfo]], Sequence[OverrideIssue]] | None = None


class AdminService(Protocol):
    @property
    def database(self) -> str | None: ...
    async def ready(self, seconds: float) -> None: ...
    async def exists(self, name: str | None = None) -> bool: ...
    async def create(
        self, name: str | None = None, *, template: str | None = None, comment: str | None = None
    ) -> None: ...
    async def create_scratch(self, name: str, *, comment: str) -> None: ...
    async def drop(self, name: str | None = None, *, force: bool = False) -> None: ...
    async def list(self, prefix: str) -> Sequence[DatabaseInfo]: ...
    def with_database(self, name: str) -> str: ...


# what a session holds on its migrated template until it closes it
class TemplateHold(Protocol):
    async def close(self) -> None: ...


# `cache` makes a named directory under the test runner's cache, and is `None` when the runner has no cache
type CacheDirectory = Callable[[str], Path]


class ProvisionerFactory(Protocol):
    def __call__(self, dsn: str, /, *, cache: CacheDirectory | None = None) -> Provisioner: ...


class Provisioner(Protocol):
    # lines that shape the template beyond the migrations (an engine and its snapshot format), folded into its name
    @property
    def identity(self) -> Sequence[str]: ...
    async def ensure_template(
        self, migrator: MigrationService | None, name: str, *, keep_stale: bool, project: str
    ) -> TemplateHold: ...
    async def clone(self, template: str, project: str) -> str: ...
    async def drop(self, name: str) -> None: ...
    def with_database(self, name: str) -> str: ...
    async def connect(self, dsn: str) -> DatabaseConnection: ...
    def pool(self, dsn: str) -> DatabasePool: ...


class MigrationFactory(Protocol):
    def __call__(  # ruff: ignore[too-many-arguments]
        self,
        dsn: str,
        path: str | PathLike[str],
        /,
        *,
        table: str = "_rowx_migrations",
        create_schemas: Iterable[str] = (),
        allow_out_of_order: bool = False,
        ignore_missing: bool = False,
    ) -> MigrationService: ...


# `urls` holds one URL per describe worker, or a single URL that every worker shares
@dataclass(slots=True, kw_only=True, frozen=True)
class ScratchBuild:
    urls: tuple[str, ...]
    seconds: float


# builds the database to describe against, and tears it down when the session ends; `sweep` is the last resort from
# outside the event loop, for a teardown that was interrupted
# `workers` is how many describe workers will run, so that a scratch that needs one database per worker builds no more
class ScratchService(Protocol):
    def session(self, migrator: MigrationService, workers: int) -> AbstractAsyncContextManager[ScratchBuild]: ...
    def sweep(self) -> None: ...


# raised by an embedded factory for a URL it refuses; the message never quotes a value from the URL, so it can be shown
class EmbeddedTargetError(ValueError):
    pass


# a database that the backend runs inside the process: the caller never connects to a server, it asks the service for
# the DSN of an instance that it owns while the context is open
class EmbeddedService(Protocol):
    # True when the target is a directory that outlives the run, False for a fresh in-memory database
    @property
    def persistent(self) -> bool: ...
    # starts the persistent instance once and yields its DSN; the instance is closed when the context exits
    def open(self) -> AbstractAsyncContextManager[str]: ...
    # an in-memory scratch with one instance per worker
    def scratch(self) -> ScratchService: ...
    # the last resort from outside the event loop, for an instance that an interrupted run could not close in time
    def sweep(self) -> None: ...


class UnsupportedCapabilityError(Exception):
    def __init__(self, capability: str) -> None:
        super().__init__(f"the selected backend does not support {capability}")


@dataclass(slots=True, kw_only=True, frozen=True)
class Backend:
    checker_factory: Callable[[], CheckerBackend] | None = None
    admin_factory: Callable[[str], AdminService] | None = None
    migration_factory: MigrationFactory | None = None
    testing_factory: ProvisionerFactory | None = None
    # `None` from the factory means the URL is not an embedded one
    embedded_factory: Callable[[str], EmbeddedService | None] | None = None

    def checker(self) -> CheckerBackend:
        if self.checker_factory is None:
            capability = "query checking"
            raise UnsupportedCapabilityError(capability)
        return self.checker_factory()

    def admin(self, dsn: str) -> AdminService:
        if self.admin_factory is None:
            capability = "database administration"
            raise UnsupportedCapabilityError(capability)
        return self.admin_factory(dsn)

    def migrations(  # ruff: ignore[too-many-arguments]
        self,
        dsn: str,
        path: str | PathLike[str],
        /,
        *,
        table: str = "_rowx_migrations",
        create_schemas: Iterable[str] = (),
        allow_out_of_order: bool = False,
        ignore_missing: bool = False,
    ) -> MigrationService:
        if self.migration_factory is None:
            capability = "migrations"
            raise UnsupportedCapabilityError(capability)
        return self.migration_factory(
            dsn,
            path,
            table=table,
            create_schemas=create_schemas,
            allow_out_of_order=allow_out_of_order,
            ignore_missing=ignore_missing,
        )

    def testing(self, dsn: str, *, cache: CacheDirectory | None = None) -> Provisioner:
        if self.testing_factory is None:
            capability = "test database provisioning"
            raise UnsupportedCapabilityError(capability)
        return self.testing_factory(dsn, cache=cache)

    def embedded(self, dsn: str) -> EmbeddedService | None:
        if self.embedded_factory is None:
            return None
        return self.embedded_factory(dsn)
