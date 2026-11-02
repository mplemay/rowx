from dataclasses import dataclass
from typing import NamedTuple, NotRequired, TypedDict

from rowx import query


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
    b: int = 0


@dataclass
class WithDefault:
    id: int
    name: str
    email: str | None = None


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


@dataclass
class Counted:
    total: int


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


def run_ok() -> None:
    query[User](t"SELECT email, name, id FROM users")
    query[User](t"SELECT id, name, email FROM users")
    query[WithDefault](t"SELECT id, name FROM users")
    query[Counted](t'SELECT count(*) AS "total!" FROM users')
    query[Counted](t'SELECT count(*) AS "total!" FROM users GROUP BY name')
    query[Pair](t"SELECT id, name FROM users")
    query[Trailing](t"SELECT id FROM users")
    query[Trailing](t"SELECT id, name FROM users")
    query[Trailing](t"SELECT id, name, email FROM users")
    query[Movie](t"SELECT id, name FROM users")
    query[Movie](t"SELECT name, id, email FROM users")
    query[Loose](t"SELECT 1 AS a, 2 AS b")


def run_rules() -> None:
    query[User](t"SELECT id, name FROM users")  # error: [missing-column]
    query[Small](t"SELECT id, name, email FROM users")  # error: [unexpected-column]
    query[Small](t"SELECT id, id, name FROM users")  # error: [duplicate-column]
    query[Loose](t"SELECT 1, 2")  # error: [duplicate-column]
    query[Small](t"SELECT id, name, 1 FROM users")  # error: [unexpected-column]
    query[Pair](t"SELECT name, id FROM users")  # error: [column-name-mismatch, column-name-mismatch]
    query[Pair](t"SELECT id FROM users")  # error: [arity-mismatch]
    query[Trailing](t"SELECT name FROM users")  # error: [column-name-mismatch]
    query[Typed](t"SELECT id, name, email FROM users")  # error: [type-mismatch]
    query[Strict](t"SELECT id, name, email FROM users")  # error: [nullability-mismatch]
    query[Movie](t"SELECT id, name, id AS id2 FROM users")  # error: [unexpected-column]
    query[Movie](t"SELECT id FROM users")  # error: [missing-column]
