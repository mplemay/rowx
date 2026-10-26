import pickle  # ruff: ignore[suspicious-pickle-import]
from typing import Final

import pytest

import rowx
from rowx import errors
from rowx.errors import (
    ConnectionBusyError,
    DatabaseError,
    DecodeError,
    EncodeError,
    Error,
    InterfaceError,
    PoolTimeoutError,
    RowNotFound,
    RowShapeError,
)

ALL_ERRORS: Final[list[type[Error]]] = [
    InterfaceError,
    ConnectionBusyError,
    PoolTimeoutError,
    DatabaseError,
    RowNotFound,
    RowShapeError,
    DecodeError,
    EncodeError,
]


def test_root_is_exception() -> None:
    assert Error.__bases__ == (Exception,)


@pytest.mark.parametrize("cls", ALL_ERRORS)
def test_every_class_subclasses_error(cls: type[Exception]) -> None:
    assert issubclass(cls, Error)


def test_tree() -> None:
    assert issubclass(ConnectionBusyError, InterfaceError)
    assert issubclass(PoolTimeoutError, InterfaceError)
    assert InterfaceError.__bases__ == (Error,)
    for cls in (DatabaseError, RowNotFound, RowShapeError, DecodeError, EncodeError):
        assert cls.__bases__ == (Error,)


@pytest.mark.parametrize("cls", [Error, *ALL_ERRORS])
def test_reexported_from_package(cls: type[Exception]) -> None:
    assert getattr(rowx, cls.__name__) is cls
    assert cls.__name__ in errors.__all__


def test_database_error_attributes() -> None:
    err = DatabaseError("23505", "duplicate key", detail="d", hint="h", position=7)
    assert (err.sqlstate, err.message, err.detail, err.hint, err.position) == ("23505", "duplicate key", "d", "h", 7)
    assert "23505" in str(err)
    assert "duplicate key" in str(err)


def test_database_error_defaults() -> None:
    err = DatabaseError("42P01", "missing")
    assert err.detail is None
    assert err.hint is None
    assert err.position is None


def test_database_error_pickles() -> None:
    err = pickle.loads(pickle.dumps(DatabaseError("42P01", "missing", position=3)))  # ruff: ignore[suspicious-pickle-usage]
    assert isinstance(err, DatabaseError)
    assert (err.sqlstate, err.message, err.position) == ("42P01", "missing", 3)
