import string.templatelib
from string.templatelib import (
    Interpolation,
    Template,
    Template as T,  # ruff: ignore[camelcase-imported-as-acronym]
    convert,
)
from typing import Optional, cast

from rowx import query, query_unchecked

value = 1
spec = ">5"


def literals() -> None:
    Template("lit", Interpolation(value, "value"))
    Template("ab", t"{value}", Interpolation(value, "value", "r", ""))  # ty: ignore[invalid-argument-type]
    Template(Interpolation(value, "value", None, "ident"), "x")
    Template("a", Interpolation(value, "value", conversion="s", format_spec="ident"))
    string.templatelib.Template("a")
    T("a")
    t"a" + t"b"
    t"a" + Template("b")
    Template("a") + t"b"
    t"a" + (t"b" + t"c")
    convert(value, "r")


def type_level(x: object, y: Optional[Template] = None, z: list[Template] | None = None) -> bool:  # ruff: ignore[non-pep604-annotation-optional]
    annotated: Template = Template("a")
    _ = annotated + t"b"
    cast("Template", x)
    return isinstance(x, Template) or issubclass(type(x), Template) or isinstance(y, (Template, int)) or bool(z)


def evident(first: Template, second: Template | None) -> None:
    first + t"a"
    t"a" + first
    t"a" + (second or first)  # error: [template-construction]


def direct_argument_specs() -> None:
    query_unchecked[int](t"SELECT {value:ident}")
    query[int | None](t"SELECT {value}::int")
    query_unchecked[int](t"SELECT {value:>5}")
    value_spec = f"{spec}"
    t"{value:>5}" + t"{value}"
    _ = value_spec
