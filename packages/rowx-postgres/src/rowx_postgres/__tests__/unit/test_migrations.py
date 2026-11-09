from typing import TYPE_CHECKING, Final

import pytest
from rowx_core import MigrationSourceError
from rowx_core.migrations import discover_migrations
from rowx_postgres import Migrator

if TYPE_CHECKING:
    from pathlib import Path

VALID_SOURCES: Final[tuple[tuple[tuple[str, bytes], ...], ...]] = (
    (),
    (("0_init.sql", b"SELECT 0;"),),
    (("007_a__b.sql", b"SELECT 1;\r\n"),),
    (("3_.sql", b""),),
    (("9223372036854775807_max.sql", b"SELECT 1;"),),
    (("README.md", b"\xff"), ("2_b.SQL", b"ignored"), ("1_a.sql", b"SELECT 1;")),
    (("2_z.sql", b"SELECT 2;"), ("1_a.up.sql", b"SELECT 1;"), ("1_other.down.sql", b"SELECT 0;")),
    (("1_a.sql", b"-- no-transaction: reason\nSELECT 1;"),),
    (("1_a.sql", b"\xef\xbb\xbfSELECT 1;"),),
    (("1_a.sql", b"\n-- no-transaction\nSELECT 1;"),),
)
INVALID_SOURCES: Final[tuple[tuple[tuple[str, bytes], ...], ...]] = (
    (("users.sql", b""),),
    (("1.sql", b""),),
    (("1.up.sql", b""),),
    (("_a.sql", b""),),
    (("-1_a.sql", b""),),
    (("+1_a.sql", b""),),
    (("\u0661_a.sql", b""),),
    (("9223372036854775808_a.sql", b""),),
    (("1_a.sql", b""), ("001_b.sql", b"")),
    (("1_a.sql", b""), ("1_b.up.sql", b""), ("1_b.down.sql", b"")),
    (("1_a.up.sql", b""),),
    (("1_a.down.sql", b""),),
    (("1_a.sql", b"\xff"),),
)


@pytest.mark.parametrize("files", VALID_SOURCES)
def test_discover_migrations_matches_native_metadata(tmp_path: Path, files: tuple[tuple[str, bytes], ...]) -> None:
    for name, content in files:
        (tmp_path / name).write_bytes(content)
    assert discover_migrations(tmp_path) == Migrator(tmp_path).migrations


@pytest.mark.parametrize("files", INVALID_SOURCES)
def test_discover_migrations_matches_native_errors(tmp_path: Path, files: tuple[tuple[str, bytes], ...]) -> None:
    for name, content in files:
        (tmp_path / name).write_bytes(content)
    with pytest.raises(MigrationSourceError) as native:
        Migrator(tmp_path)
    with pytest.raises(MigrationSourceError) as python:
        discover_migrations(tmp_path)
    assert str(python.value) == str(native.value)
    assert python.value.path == native.value.path
    assert python.value.version == native.value.version


def test_discover_migrations_follows_file_symlinks_and_ignores_directories(tmp_path: Path) -> None:
    target = tmp_path / "source.txt"
    target.write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "1_link.sql").symlink_to(target)
    (tmp_path / "2_directory.sql").mkdir()
    (tmp_path / "ignored.md").symlink_to(tmp_path / "absent")
    assert discover_migrations(tmp_path) == Migrator(tmp_path).migrations


def test_discover_migrations_rejects_dangling_sql_symlink(tmp_path: Path) -> None:
    (tmp_path / "1_link.sql").symlink_to(tmp_path / "absent")
    with pytest.raises(MigrationSourceError):
        Migrator(tmp_path)
    with pytest.raises(MigrationSourceError):
        discover_migrations(tmp_path)
