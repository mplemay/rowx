from typing import TYPE_CHECKING

from rowx.check.config import DEFAULT_MIGRATE_TABLE
from rowx.postgres import Migrator
from rowx.testing import TEMPLATE_PREFIX, project_prefix, template_name

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path


def migrations(path: Path) -> Path:
    path.mkdir()
    (path / "1_a.sql").write_text("SELECT 1;", encoding="utf-8")
    return path


def name_of(path: Path, *, table: str = DEFAULT_MIGRATE_TABLE, create_schemas: Iterable[str] = ()) -> str:
    return template_name(Migrator(path), directory=path, table=table, create_schemas=create_schemas)


def test_template_name_changes_with_the_rowx_version(tmp_path: Path) -> None:
    path = migrations(tmp_path / "migrations")
    first = template_name(Migrator(path), directory=path, rowx="1.0.0")
    assert template_name(Migrator(path), directory=path, rowx="1.0.0") == first
    assert template_name(Migrator(path), directory=path, rowx="1.0.1") != first
    # only the `<h>` half changes: the project prefix is the path alone
    assert template_name(Migrator(path), directory=path, rowx="1.0.1").split("_")[2] == first.split("_")[2]


def test_template_name_changes_with_the_path(tmp_path: Path) -> None:
    first = migrations(tmp_path / "one")
    second = migrations(tmp_path / "two")
    one = template_name(Migrator(first), directory=first, rowx="1.0.0")
    two = template_name(Migrator(second), directory=second, rowx="1.0.0")
    # identical files, different paths: a different project prefix and a different hash
    assert one != two
    assert one.split("_")[2] != two.split("_")[2]
    assert one.split("_")[3] != two.split("_")[3]
    assert one.startswith(f"{TEMPLATE_PREFIX}{project_prefix(first)}_")


def test_template_name_follows_the_migrations_and_their_config(tmp_path: Path) -> None:
    path = migrations(tmp_path / "migrations")
    first = name_of(path)
    assert name_of(path) == first
    (path / "2_b.up.sql").write_text("SELECT 2;", encoding="utf-8")
    (path / "2_b.down.sql").write_text("SELECT 3;", encoding="utf-8")
    with_up = name_of(path)
    assert with_up != first
    # a down file never changes the template
    (path / "2_b.down.sql").write_text("SELECT 5;", encoding="utf-8")
    assert name_of(path) == with_up
    # the history table and the schemas created first are part of the template too
    others = {name_of(path, table="app.history"), name_of(path, create_schemas=["app"])}
    assert len(others) == 2
    assert with_up not in others
    assert name_of(path, create_schemas=["b", "a"]) == name_of(path, create_schemas=["a", "b", "a"])
    (path / "1_a.sql").write_text("SELECT 4;", encoding="utf-8")
    assert name_of(path) not in {first, with_up}


def test_an_empty_template_is_named_by_the_directory_alone(tmp_path: Path) -> None:
    first = template_name(None, directory=tmp_path)
    assert first.startswith(f"{TEMPLATE_PREFIX}{project_prefix(tmp_path)}_")
    assert template_name(None, directory=tmp_path, rowx="9.9.9") != first
