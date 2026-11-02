import os
from pathlib import Path
from typing import Final

import pytest

from rowx.check.cli import main

pytestmark = [pytest.mark.integration, pytest.mark.db]

EXPECTED: Final[Path] = Path(__file__).resolve().parent / "expected"
MODELS: Final[str] = """\
from dataclasses import dataclass
from typing import NamedTuple, NotRequired, TypedDict


@dataclass
class User:
    id: int
    name: str
    email: str | None


@dataclass
class Small:
    id: int
    name: str


@dataclass
class Loose:
    a: int = 0


@dataclass
class Typed:
    id: str
    name: str
    email: str | None


@dataclass
class Strict:
    id: int
    name: str
    email: str


class Pair(NamedTuple):
    id: int
    name: str


class Trailing(NamedTuple):
    id: int
    name: str = ""
    email: str | None = None


class Movie(TypedDict):
    id: int
    name: str
    email: NotRequired[str | None]
"""
QUERIES: Final[str] = """\
from app.models import Loose, Movie, Pair, Small, Strict, Trailing, Typed, User
from rowx import query

query[User](t"SELECT id FROM users")
query[Small](t"SELECT id, name, email FROM users")
query[Small](t"SELECT id, id, name FROM users")
query[Loose](t"SELECT 1, 2")
query[Pair](t"SELECT name, id FROM users")
query[Pair](t"SELECT id FROM users")
query[Trailing](t"SELECT id, name, email, 1 FROM users")
query[Typed](t"SELECT id, name, email FROM users")
query[Strict](t"SELECT id, name, email FROM users")
query[Movie](t"SELECT id, name, id AS other FROM users")
query[User](t"SELECT email, name, id FROM users")
query[Trailing](t"SELECT id FROM users")
query[Movie](t"SELECT id, name FROM users")
query[Small](t'SELECT id, name AS "name!" FROM users')
"""


def golden(name: str, actual: str) -> None:
    path = EXPECTED / name
    if "ROWX_UPDATE_GOLDENS" in os.environ:
        path.write_text(actual, encoding="utf-8")
    assert actual == path.read_text(encoding="utf-8")


def run(tmp_path: Path, schema: str, capsys: pytest.CaptureFixture[str], *argv: str) -> str:
    package = tmp_path / "app"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "models.py").write_text(MODELS, encoding="utf-8")
    (tmp_path / "queries.py").write_text(QUERIES, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text('[tool.rowx]\ninclude = ["."]\n', encoding="utf-8")
    code = main([
        "check",
        str(tmp_path / "queries.py"),
        "--config",
        str(tmp_path / "pyproject.toml"),
        "--database-url",
        schema,
        *argv,
    ])
    assert code == 1
    out = capsys.readouterr().out
    # the notes name the files by their full path
    return out.replace(str(tmp_path.resolve()), "<root>").replace(str(tmp_path), "<root>")


def test_named_rows_text(tmp_path: Path, schema: str, capsys: pytest.CaptureFixture[str]) -> None:
    golden("named_rows.txt", run(tmp_path, schema, capsys))


def test_named_rows_json(tmp_path: Path, schema: str, capsys: pytest.CaptureFixture[str]) -> None:
    golden("named_rows.json.txt", run(tmp_path, schema, capsys, "--output-format", "json"))
