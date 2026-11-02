from typing import TYPE_CHECKING, Final, Self, override

if TYPE_CHECKING:
    from pathlib import Path

    from rowx.postgres import MigrationResult

__all__: Final[list[str]] = [
    "ConnectionBusyError",
    "DatabaseError",
    "DecodeError",
    "DirtyMigrationError",
    "EncodeError",
    "Error",
    "InterfaceError",
    "IrreversibleMigrationError",
    "MigrateError",
    "MigrationChecksumError",
    "MigrationFailedError",
    "MigrationMissingError",
    "MigrationOrderError",
    "MigrationSourceError",
    "PoolTimeoutError",
    "RowNotFound",
    "RowShapeError",
]


class Error(Exception):
    pass


class InterfaceError(Error):
    pass


class ConnectionBusyError(InterfaceError):
    pass


class PoolTimeoutError(InterfaceError):
    pass


class DatabaseError(Error):
    sqlstate: str
    message: str
    detail: str | None
    hint: str | None
    position: int | None

    def __init__(
        self,
        sqlstate: str,
        message: str,
        detail: str | None = None,
        hint: str | None = None,
        position: int | None = None,
    ) -> None:
        super().__init__(sqlstate, message)
        self.sqlstate = sqlstate
        self.message = message
        self.detail = detail
        self.hint = hint
        self.position = position

    @override
    def __str__(self) -> str:
        return f"{self.sqlstate}: {self.message}"

    @override
    def __reduce__(self) -> tuple[type[Self], tuple[str, str, str | None, str | None, int | None]]:
        return type(self), (self.sqlstate, self.message, self.detail, self.hint, self.position)


class RowNotFound(Error):  # ruff: ignore[error-suffix-on-exception-name]
    pass


class RowShapeError(Error):
    pass


class DecodeError(Error):
    pass


class EncodeError(Error):
    pass


class MigrateError(Error):
    pass


class MigrationSourceError(MigrateError):
    path: Path | None
    version: int | None

    def __init__(self, message: str, path: Path | None = None, version: int | None = None) -> None:
        super().__init__(message)
        self.path = path
        self.version = version

    @override
    def __reduce__(self) -> tuple[type[Self], tuple[str, Path | None, int | None]]:
        return type(self), (str(self), self.path, self.version)


class IrreversibleMigrationError(MigrationSourceError):
    def __init__(self, message: str, version: int) -> None:
        super().__init__(message, None, version)

    @override
    def __reduce__(self) -> tuple[type[Self], tuple[str, int | None]]:  # ty: ignore[invalid-method-override]
        return type(self), (str(self), self.version)


class MigrationChecksumError(MigrateError):
    version: int
    local_checksum: bytes
    applied_checksum: bytes

    def __init__(self, message: str, version: int, local_checksum: bytes, applied_checksum: bytes) -> None:
        super().__init__(message)
        self.version = version
        self.local_checksum = local_checksum
        self.applied_checksum = applied_checksum

    @override
    def __reduce__(self) -> tuple[type[Self], tuple[str, int, bytes, bytes]]:
        return type(self), (str(self), self.version, self.local_checksum, self.applied_checksum)


class MigrationMissingError(MigrateError):
    versions: tuple[int, ...]

    def __init__(self, message: str, versions: tuple[int, ...]) -> None:
        super().__init__(message)
        self.versions = versions

    @override
    def __reduce__(self) -> tuple[type[Self], tuple[str, tuple[int, ...]]]:
        return type(self), (str(self), self.versions)


class MigrationOrderError(MigrateError):
    version: int
    latest: int

    def __init__(self, message: str, version: int, latest: int) -> None:
        super().__init__(message)
        self.version = version
        self.latest = latest

    @override
    def __reduce__(self) -> tuple[type[Self], tuple[str, int, int]]:
        return type(self), (str(self), self.version, self.latest)


class DirtyMigrationError(MigrateError):
    version: int

    def __init__(self, message: str, version: int) -> None:
        super().__init__(message)
        self.version = version

    @override
    def __reduce__(self) -> tuple[type[Self], tuple[str, int]]:
        return type(self), (str(self), self.version)


# `__cause__` is `error`; pickling drops it, so `error` is the copy that survives
class MigrationFailedError(MigrateError):
    version: int
    path: Path | None
    dirty: bool
    applied: tuple[MigrationResult, ...]
    error: DatabaseError

    def __init__(  # ruff: ignore[too-many-arguments, too-many-positional-arguments]
        self,
        message: str,
        version: int,
        path: Path | None,
        dirty: bool,  # ruff: ignore[boolean-type-hint-positional-argument]
        applied: tuple[MigrationResult, ...],
        error: DatabaseError,
    ) -> None:
        super().__init__(message)
        self.version = version
        self.path = path
        self.dirty = dirty
        self.applied = applied
        self.error = error

    @override
    def __reduce__(
        self,
    ) -> tuple[type[Self], tuple[str, int, Path | None, bool, tuple[MigrationResult, ...], DatabaseError]]:
        return type(self), (str(self), self.version, self.path, self.dirty, self.applied, self.error)
