from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator
from pydantic.dataclasses import dataclass

from rowx import query


class User(BaseModel):
    id: int
    name: str
    email: str | None


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
    name: str


class Mixed(BaseModel):
    id: int = Field(validation_alias=AliasChoices("a", "b"))
    name: int


class Rewritten(BaseModel):
    id: int
    name: int

    @model_validator(mode="before")
    @classmethod
    def pre(cls, data: object) -> object:
        return data


class Generated(BaseModel):
    model_config = ConfigDict(alias_generator=str.upper)

    id: int
    name: int


class Score(BaseModel):
    id: int
    score: int


@dataclass(config=ConfigDict(populate_by_name=True))
class Row:
    id: int = Field(alias="user_id")
    name: str


def run_ok() -> None:
    query[User](t"SELECT email, name, id FROM users")
    query[Aliased](t"SELECT id AS user_id, name FROM users")
    query[Either](t"SELECT id AS user_id, name FROM users")
    query[Either](t"SELECT id, name FROM users")
    query[Choices](t"SELECT id AS a, name FROM users")
    query[Choices](t"SELECT id AS b, name, 1 AS extra FROM users")
    query[Rewritten](t"SELECT id, name AS other FROM users")
    query[Generated](t"SELECT id, name FROM users")
    query[Row](t"SELECT id, name FROM users")


def run_rules() -> None:
    query[Aliased](t"SELECT id, name FROM users")  # error: [unexpected-column, missing-column]
    query[Either](t"SELECT id, id AS user_id, name FROM users")  # error: [duplicate-column]
    query[Overlap](t"SELECT id AS y FROM users")  # error: [ambiguous-column]
    query[Score](t"SELECT id, 9.5::numeric AS score FROM users")  # error: [type-mismatch]
    query[Mixed](t"SELECT id AS a, name FROM users")  # error: [type-mismatch]
