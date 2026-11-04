from string.templatelib import (
    Interpolation,
    Template,
    Template as T,  # ruff: ignore[camelcase-imported-as-acronym]
)

from rowx import query_unchecked

parts = ["a", "b"]
kwargs = {"a": 1}
text = "SELECT 1"
spec = "ident"
value = 1


def build() -> None:
    Template(text)  # error: [template-construction]
    Template(*parts)  # error: [template-construction]
    Template(f"SELECT {text}")  # error: [template-construction]
    Template("a", text)  # error: [template-construction]
    Template(text="a")  # error: [template-construction]  # ty: ignore[unknown-argument]
    Template(**kwargs)  # error: [template-construction]
    T(text)  # error: [template-construction]
    t"a" + Template(text)  # error: [template-construction]
    Interpolation(value, "value", None, spec)  # error: [template-construction]
    Interpolation(value, "value", conversion=text)  # error: [template-construction]  # ty: ignore[invalid-argument-type]
    Interpolation(value, "value", format_spec=spec)  # error: [template-construction]
    Interpolation(*parts)  # error: [template-construction]  # ty: ignore[invalid-argument-type]
    Interpolation(**kwargs)  # error: [template-construction]  # ty: ignore[invalid-argument-type]
    _ = t"{value:{'ident'}}"  # error: [template-construction]
    t"a" + text  # error: [template-construction]  # ty: ignore[unsupported-operator]
    text + t"a"  # error: [template-construction]  # ty: ignore[unsupported-operator]
    t"a" + t"b" + text  # error: [template-construction]  # ty: ignore[unsupported-operator]


def spec_in_a_helper(dynamic: str):
    return t"SELECT {value:{dynamic}}"  # error: [template-construction]


def nested_into_query_unchecked(dynamic: str) -> None:
    query_unchecked[int](t"SELECT {t'{value:{dynamic}}'}")  # error: [template-construction]
    query_unchecked[int](t"SELECT {value:{dynamic}}")  # error: [dynamic-query]
