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


class Doc(TypedDict):
    id: int
    slug: str


class Labeled(TypedDict):
    id: str
    name: str
    email: str


class Triple(NamedTuple):
    id: int
    name: str
    email: str | None
"""
QUERIES: Final[str] = """\
from app.models import Doc, Labeled, Loose, Movie, Pair, Small, Strict, Trailing, Triple, Typed, User
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
query[Doc](t"SELECT id FROM users")
query[Small](t"SELECT id, name, 1 FROM users")
query[Small](t'SELECT id AS "id!!", name FROM users')
query[Triple](t"SELECT id, email, name FROM users")
query[Labeled](t"SELECT id, name, email FROM users")
"""


PYDANTIC_MODELS: Final[str] = """\
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator
from pydantic.dataclasses import dataclass


class Aliased(BaseModel):
    id: int = Field(alias="user_id")
    name: str


class Either(BaseModel, populate_by_name=True):
    id: int = Field(alias="user_id")
    name: str


class Overlap(BaseModel):
    x: int = Field(alias="y")
    y: int


class Choices(BaseModel):
    id: int = Field(validation_alias=AliasChoices("a", "b"))
    name: int


class Rewritten(BaseModel):
    id: int
    name: int

    @model_validator(mode="before")
    @classmethod
    def pre(cls, data: object) -> object:
        return data


class Score(BaseModel):
    id: int
    score: int


@dataclass(config=ConfigDict(populate_by_name=True))
class Row:
    id: int = Field(alias="user_id")
    name: str
"""
PYDANTIC_QUERIES: Final[str] = """\
from app.models import Aliased, Choices, Either, Overlap, Rewritten, Row, Score
from rowx import query

query[Aliased](t"SELECT id, name FROM users")
query[Aliased](t"SELECT id AS user_id, name FROM users")
query[Either](t"SELECT id AS user_id, name FROM users")
query[Either](t"SELECT id, name FROM users")
query[Either](t"SELECT id, id AS user_id, name FROM users")
query[Overlap](t"SELECT id AS y FROM users")
query[Choices](t"SELECT id AS a, name FROM users")
query[Choices](t"SELECT id AS b, name, 1 AS extra FROM users")
query[Rewritten](t"SELECT id, name FROM users")
query[Score](t"SELECT id, 9.5::numeric AS score FROM users")
query[Row](t"SELECT id, id AS user_id, name FROM users")
"""


def golden(name: str, actual: str) -> None:
    path = EXPECTED / name
    if "ROWX_UPDATE_GOLDENS" in os.environ:
        path.write_text(actual, encoding="utf-8")
    assert actual == path.read_text(encoding="utf-8")


def run(
    tmp_path: Path,
    schema: str,
    capsys: pytest.CaptureFixture[str],
    *argv: str,
    models: str = MODELS,
    queries: str = QUERIES,
) -> str:
    package = tmp_path / "app"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "models.py").write_text(models, encoding="utf-8")
    (tmp_path / "queries.py").write_text(queries, encoding="utf-8")
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


def test_pydantic_rows_text(tmp_path: Path, schema: str, capsys: pytest.CaptureFixture[str]) -> None:
    golden("pydantic_rows.txt", run(tmp_path, schema, capsys, models=PYDANTIC_MODELS, queries=PYDANTIC_QUERIES))


def test_pydantic_rows_json(tmp_path: Path, schema: str, capsys: pytest.CaptureFixture[str]) -> None:
    out = run(tmp_path, schema, capsys, "--output-format", "json", models=PYDANTIC_MODELS, queries=PYDANTIC_QUERIES)
    golden("pydantic_rows.json.txt", out)
