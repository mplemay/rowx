import dataclasses
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from rowx_core.errors import MigrationSourceError
from rowx_postgres import Migration, MigrationResult, MigrationStatus, Migrator
from rowx_postgres._migrate import status_from


def write(directory: Path, name: str, text: str = "select 1;") -> None:
    (directory / name).write_text(text, encoding="utf-8")


def test_resolves_files_without_a_database(tmp_path: Path) -> None:
    write(tmp_path, "20260101000000_create_users.up.sql", "create table users ();")
    write(tmp_path, "20260101000000_create_users.down.sql", "drop table users;")
    write(tmp_path, "20260102000000_add_index.sql", "-- no-transaction\ncreate index concurrently i on users (id);")
    (tmp_path / "notes.txt").write_text("ignored", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    write(tmp_path / "nested", "bad name.sql")

    migrations = Migrator(tmp_path).migrations

    assert isinstance(migrations, tuple)
    assert [(m.version, m.description, m.kind, m.no_transaction) for m in migrations] == [
        (20260101000000, "create users", "reversible", False),
        (20260102000000, "add index", "simple", True),
    ]
    assert all(isinstance(m.path, Path) and m.path.parent == tmp_path for m in migrations)
    assert all(len(m.checksum) == 48 for m in migrations)


def test_a_reversible_pair_is_one_migration_with_both_files(tmp_path: Path) -> None:
    write(tmp_path, "1_a.up.sql", "create table a ();")
    write(tmp_path, "1_a.down.sql", "drop table a;")
    write(tmp_path, "2_b.sql", "select 2;")

    reversible, simple = Migrator(tmp_path).migrations

    assert reversible.kind == "reversible"
    assert reversible.path == tmp_path / "1_a.up.sql"
    assert reversible.down_path == tmp_path / "1_a.down.sql"
    assert reversible.checksum == hashlib.sha384(b"create table a ();").digest()
    assert reversible.down_checksum == hashlib.sha384(b"drop table a;").digest()
    assert (simple.kind, simple.down_path, simple.down_checksum) == ("simple", None, None)


def test_version_zero_and_leading_zeros_are_accepted(tmp_path: Path) -> None:
    write(tmp_path, "0_init.sql")
    write(tmp_path, "007_x.sql")

    assert [(m.version, m.description) for m in Migrator(tmp_path).migrations] == [(0, "init"), (7, "x")]


def test_a_leading_zero_version_duplicates_the_plain_one(tmp_path: Path) -> None:
    write(tmp_path, "1_a.sql")
    write(tmp_path, "001_b.sql")

    with pytest.raises(MigrationSourceError):
        Migrator(tmp_path)


def test_accepts_a_str_path_and_checksums_the_raw_bytes(tmp_path: Path) -> None:
    write(tmp_path, "1_a.sql")

    (migration,) = Migrator(str(tmp_path)).migrations

    assert migration.checksum == hashlib.sha384(b"select 1;").digest()


def test_the_path_is_resolved_against_the_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "migrations").mkdir()
    monkeypatch.chdir(tmp_path)

    migrator = Migrator("migrations")
    monkeypatch.chdir(tmp_path / "migrations")

    assert migrator.path == tmp_path / "migrations"
    assert Migrator(tmp_path / "migrations").path == tmp_path / "migrations"


def test_an_empty_directory_has_no_migrations(tmp_path: Path) -> None:
    assert Migrator(tmp_path).migrations == ()


@pytest.mark.parametrize(
    "name",
    ["not_a_version.sql", "-1_negative.sql", "1.sql", "99999999999999999999_big.sql"],
)
def test_a_badly_named_file_is_a_source_error_naming_the_file(tmp_path: Path, name: str) -> None:
    write(tmp_path, name)

    with pytest.raises(MigrationSourceError, match=name.replace(".", r"\.")):
        Migrator(tmp_path)


def test_a_duplicate_version_is_a_source_error_with_the_version(tmp_path: Path) -> None:
    write(tmp_path, "7_a.sql")
    write(tmp_path, "7_b.sql")

    with pytest.raises(MigrationSourceError) as exc:
        Migrator(tmp_path)

    assert exc.value.version == 7


def test_an_unpaired_up_or_down_file_is_a_source_error_with_the_version(tmp_path: Path) -> None:
    write(tmp_path, "3_a.up.sql")
    with pytest.raises(MigrationSourceError, match=r"3_a\.up\.sql") as up:
        Migrator(tmp_path)
    assert up.value.version == 3

    (tmp_path / "3_a.up.sql").unlink()
    write(tmp_path, "3_a.down.sql")
    with pytest.raises(MigrationSourceError, match=r"3_a\.down\.sql") as down:
        Migrator(tmp_path)
    assert down.value.version == 3


def test_a_non_utf8_file_is_a_source_error(tmp_path: Path) -> None:
    (tmp_path / "1_a.sql").write_bytes(b"select '\xff';")

    with pytest.raises(MigrationSourceError, match=r"1_a\.sql"):
        Migrator(tmp_path)


def test_a_missing_directory_is_a_source_error(tmp_path: Path) -> None:
    with pytest.raises(MigrationSourceError):
        Migrator(tmp_path / "absent")


@pytest.mark.parametrize(
    "table",
    ["", "a.b.c", ".m", "a.", "a\0b", "x" * 64, "Mixed Case", 'we"ird', "1abc", '"open', '"a"b', '""'],
)
def test_a_bad_table_is_a_value_error(tmp_path: Path, table: str) -> None:
    with pytest.raises(ValueError, match=r"name|identifier|empty|NUL|bytes|quote"):
        Migrator(tmp_path, table=table)


@pytest.mark.parametrize("schema", ["", "a.b", "my app", "x" * 64, '"open'])
def test_a_bad_create_schema_is_a_value_error(tmp_path: Path, schema: str) -> None:
    with pytest.raises(ValueError, match=r"schema"):
        Migrator(tmp_path, create_schemas=["ok", schema])


@pytest.mark.parametrize(
    ("table", "parts"),
    [
        ("_rowx_migrations", ("_rowx_migrations",)),
        ("app._rowx_migrations", ("app", "_rowx_migrations")),
        ("MySchema.Migrations", ("myschema", "migrations")),
        ('"My"."T"', ("My", "T")),
        ('"We""ird"', ('We"ird',)),
        ('"Mixed Case"', ("Mixed Case",)),
        ('"a.b"', ("a.b",)),
    ],
)
def test_a_valid_table_is_accepted(tmp_path: Path, table: str, parts: tuple[str, ...]) -> None:
    migrator = Migrator(
        tmp_path,
        table=table,
        create_schemas=["app"],
        locking=False,
        ignore_missing=True,
        allow_out_of_order=True,
    )
    # the parsed parts, as the server stores them, are what callers quote
    assert migrator._impl.table == parts


def test_create_schemas_must_not_be_a_bare_string(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        Migrator(tmp_path, create_schemas="app")


def test_options_are_keyword_only(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        Migrator(tmp_path, "_rowx_migrations")  # type: ignore[misc]  # ty: ignore[too-many-positional-arguments]


def test_the_dataclasses_are_frozen_slotted_and_keyword_only(tmp_path: Path) -> None:
    write(tmp_path, "1_a.sql")
    (migration,) = Migrator(tmp_path).migrations
    status = MigrationStatus(version=1, description="a", state="pending")
    result = MigrationResult(migration=migration, elapsed=None)

    for instance in (migration, status):
        assert not hasattr(instance, "__dict__")
        with pytest.raises(dataclasses.FrozenInstanceError):
            instance.version = 2  # type: ignore[misc]  # ty: ignore[invalid-assignment]
    with pytest.raises(TypeError):
        Migration(1, "a", "simple", Path(), None, b"", None, False)  # ruff: ignore[boolean-positional-value-in-call]  # type: ignore[misc]  # ty: ignore[too-many-positional-arguments, missing-argument]
    with pytest.raises(TypeError):
        MigrationStatus(1, "a", "pending")  # type: ignore[misc]  # ty: ignore[too-many-positional-arguments, missing-argument]
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.elapsed = timedelta()  # type: ignore[misc]  # ty: ignore[invalid-assignment]
    assert not hasattr(result, "__dict__")
    with pytest.raises(TypeError):
        MigrationResult(migration, None)  # type: ignore[misc]  # ty: ignore[too-many-positional-arguments, missing-argument]
    assert (status.kind, status.local_checksum, status.applied_checksum) == (None, None, None)
    assert status.installed_on is None
    assert status.execution_time is None


def test_a_status_row_becomes_a_status() -> None:
    status = status_from((5, "five", "applied", 1_700_000_000_123_456, 2_500_000, b"x"), None)

    assert status.installed_on == datetime(2023, 11, 14, 22, 13, 20, 123456, tzinfo=UTC)
    assert status.execution_time == timedelta(milliseconds=2.5)
    assert (status.kind, status.local_checksum, status.applied_checksum) == (None, None, b"x")


def test_a_status_takes_its_kind_and_local_checksum_from_the_local_migration(tmp_path: Path) -> None:
    write(tmp_path, "5_five.up.sql")
    write(tmp_path, "5_five.down.sql")
    (local,) = Migrator(tmp_path).migrations

    status = status_from((5, "five", "modified", 0, 1, b"old"), local)

    assert (status.kind, status.local_checksum, status.applied_checksum) == ("reversible", local.checksum, b"old")


def test_an_unknown_execution_time_is_none() -> None:
    assert status_from((5, "five", "applied", 0, -1, b""), None).execution_time is None
    assert status_from((5, "five", "pending", None, None, None), None) == MigrationStatus(
        version=5,
        description="five",
        state="pending",
    )
