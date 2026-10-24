from typing import Final, Self, override

__all__: Final[list[str]] = [
    "ConnectionBusyError",
    "DatabaseError",
    "DecodeError",
    "EncodeError",
    "Error",
    "InterfaceError",
    "RowNotFound",
    "RowShapeError",
]


class Error(Exception):
    pass


class InterfaceError(Error):
    pass


class ConnectionBusyError(InterfaceError):
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
