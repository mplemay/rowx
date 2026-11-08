import string.templatelib
from string.templatelib import (
    Interpolation,
    Template,
    Template as T,  # ruff: ignore[camelcase-imported-as-acronym]
    convert,
)
from typing import TYPE_CHECKING, Annotated, Optional, cast

from rowx_postgres._compiler import _is_ident_char as is_ident_char

from rowx import query, query_unchecked

if TYPE_CHECKING:
    from collections.abc import Callable

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


def types_and_inspection(
    x: object,
    parts: dict[str, Template],
    call: Callable[[Template], int],
    marked: Annotated[Template, 1],
) -> int:
    issubclass(Template, object)
    issubclass(Template, (Template, int))
    _ = marked
    cast(Template, x)  # ruff: ignore[runtime-cast-value]
    cast(list[Template], x)  # ruff: ignore[runtime-cast-value]
    cast(dict[str, Template], x)  # ruff: ignore[runtime-cast-value]
    isinstance(x, Template | int)
    isinstance(x, (Template, list[Template]))
    match x:
        case Template(strings=strings):
            return len(strings)
        case Interpolation(value=_):
            return 0
        case string.templatelib.Template():
            return 1
        case string.templatelib.Template:
            return 2
    return len(parts) + call(Template("a"))


def evident_through_assignment(first: Template) -> None:
    second = first
    third = second
    fourth = Template("a")
    t"a" + second
    third + t"a"
    t"a" + fourth


def private_function_from_a_private_module(char: str) -> bool:
    return is_ident_char(char)


def compare_with_the_class(value: object) -> bool:
    name = type(value).__name__
    return type(value) is Template or type(value) is not string.templatelib.Template or bool(name)
