import pickle  # ruff: ignore[suspicious-pickle-import]
from datetime import timedelta
from pathlib import Path
from typing import Final

import pytest
import rowx_core as rowx
from rowx_core import errors
from rowx_core.errors import (
    ConnectionBusyError,
    DatabaseError,
    DecodeError,
    DirtyMigrationError,
    EncodeError,
    Error,
    InterfaceError,
    IrreversibleMigrationError,
    MigrateError,
    MigrationChecksumError,
    MigrationFailedError,
    MigrationMissingError,
    MigrationOrderError,
    MigrationSourceError,
    PoolTimeoutError,
    RowConstructError,
    RowNotFound,
    RowShapeError,
)
from rowx_core.migrations import Migration, MigrationResult

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


def test_row_construct_error_is_a_decode_error_with_attributes() -> None:
    err = RowConstructError("User row 3: ValueError('x')", "User", 3)
    assert isinstance(err, DecodeError)
    assert (str(err), err.type_name, err.row_index) == ("User row 3: ValueError('x')", "User", 3)
    assert rowx.RowConstructError is RowConstructError


def test_row_construct_error_keyword_constructor() -> None:
    err = RowConstructError("m", type_name="User", row_index=1)
    assert (err.type_name, err.row_index) == ("User", 1)


def test_row_construct_error_pickles_without_its_cause() -> None:
    err = RowConstructError("User row 3: ValueError('x')", "User", 3)
    err.__cause__ = ValueError("x")
    clone = pickle.loads(pickle.dumps(err))  # ruff: ignore[suspicious-pickle-usage]
    assert type(clone) is RowConstructError
    assert (str(clone), clone.type_name, clone.row_index) == (str(err), "User", 3)
    assert clone.__cause__ is None


MIGRATION = Migration(
    version=3,
    description="three",
    kind="reversible",
    path=Path("3_three.up.sql"),
    down_path=Path("3_three.down.sql"),
    checksum=b"up",
    down_checksum=b"down",
    no_transaction=False,
)
FAILURE_CAUSE = DatabaseError("23505", "duplicate key", detail="d")
MIGRATION_ERRORS: Final[list[MigrateError]] = [
    MigrationSourceError("bad name", Path("x.sql"), 7),
    MigrationSourceError("no directory"),
    IrreversibleMigrationError("no down file", 4),
    MigrationChecksumError("modified", 3, b"local", b"applied"),
    MigrationMissingError("missing", (1, 2)),
    MigrationOrderError("older", 2, 5),
    DirtyMigrationError("dirty", 6),
    MigrationFailedError(
        "failed",
        3,
        Path("3_three.up.sql"),
        dirty=True,
        applied=(MigrationResult(migration=MIGRATION, elapsed=timedelta(seconds=1)),),
        error=FAILURE_CAUSE,
    ),
]


@pytest.mark.parametrize("err", MIGRATION_ERRORS, ids=lambda e: type(e).__name__)
def test_migration_errors_pickle_with_their_attributes(err: MigrateError) -> None:
    clone = pickle.loads(pickle.dumps(err))  # ruff: ignore[suspicious-pickle-usage]

    assert type(clone) is type(err)
    assert str(clone) == str(err)
    assert isinstance(clone, MigrateError)
    for name in vars(err):
        original, copied = getattr(err, name), getattr(clone, name)
        if isinstance(original, DatabaseError):
            assert (copied.sqlstate, copied.message, copied.detail) == (
                original.sqlstate,
                original.message,
                original.detail,
            )
        else:
            assert copied == original


def test_every_migration_error_is_a_migrate_error() -> None:
    assert MigrateError.__bases__ == (Error,)
    for cls in (
        MigrationSourceError,
        MigrationChecksumError,
        MigrationMissingError,
        MigrationOrderError,
        DirtyMigrationError,
        MigrationFailedError,
    ):
        assert cls.__bases__ == (MigrateError,)
    assert IrreversibleMigrationError.__bases__ == (MigrationSourceError,)


@pytest.mark.parametrize("cls", [MigrateError, *(type(e) for e in MIGRATION_ERRORS)])
def test_migration_errors_are_reexported_from_the_package(cls: type[Exception]) -> None:
    assert getattr(rowx, cls.__name__) is cls
    assert cls.__name__ in errors.__all__
    assert cls.__name__ in rowx.__all__


def test_migration_error_attributes() -> None:
    source, no_directory, irreversible, checksum, missing, order, dirty, failed = MIGRATION_ERRORS
    assert isinstance(source, MigrationSourceError)
    assert (source.path, source.version) == (Path("x.sql"), 7)
    assert isinstance(no_directory, MigrationSourceError)
    assert (no_directory.path, no_directory.version) == (None, None)
    assert isinstance(irreversible, IrreversibleMigrationError)
    assert (irreversible.path, irreversible.version) == (None, 4)
    assert isinstance(checksum, MigrationChecksumError)
    assert (checksum.version, checksum.local_checksum, checksum.applied_checksum) == (3, b"local", b"applied")
    assert isinstance(missing, MigrationMissingError)
    assert missing.versions == (1, 2)
    assert isinstance(order, MigrationOrderError)
    assert (order.version, order.latest) == (2, 5)
    assert isinstance(dirty, DirtyMigrationError)
    assert dirty.version == 6
    assert isinstance(failed, MigrationFailedError)
    assert (failed.version, failed.path, failed.dirty, failed.error) == (3, Path("3_three.up.sql"), True, FAILURE_CAUSE)
    assert [r.migration.version for r in failed.applied] == [3]


def test_migration_errors_take_keyword_arguments() -> None:
    err = MigrationChecksumError("modified", version=3, local_checksum=b"a", applied_checksum=b"b")
    assert (err.version, err.local_checksum, err.applied_checksum) == (3, b"a", b"b")
    order = MigrationOrderError(message="older", version=2, latest=5)
    assert (order.version, order.latest) == (2, 5)
    failed = MigrationFailedError(message="failed", version=1, path=None, dirty=False, applied=(), error=FAILURE_CAUSE)
    assert (failed.path, failed.dirty, failed.applied) == (None, False, ())
