from typing import Final, Self, override

__all__: Final[list[str]] = [
    "ConnectionBusyError",
    "DatabaseError",
    "DecodeError",
    "DirtyMigrationError",
    "EncodeError",
    "Error",
    "InterfaceError",
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
    version: int | None

    def __init__(self, message: str, version: int | None = None) -> None:
        super().__init__(message)
        self.version = version

    @override
    def __reduce__(self) -> tuple[type[Self], tuple[str, int | None]]:
        return type(self), (str(self), self.version)


class MigrationSourceError(MigrateError):
    pass


class MigrationChecksumError(MigrateError):
    pass


class MigrationMissingError(MigrateError):
    pass


class MigrationOrderError(MigrateError):
    pass


class DirtyMigrationError(MigrateError):
    pass


# `__cause__` is the server's DatabaseError
class MigrationFailedError(MigrateError):
    pass
