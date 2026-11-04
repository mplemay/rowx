import string.templatelib
import typing
from string import templatelib
from string.templatelib import Template, _template_unpickle  # error: [template-construction]  # ty: ignore[unresolved-import]
from typing import Annotated, cast

import rowx._template
import rowx._template as private_module
from rowx import _template as private_alias
from rowx._template import Interpolation as RowxInterpolation, Template as RowxTemplate

__all__ = ["Template"]  # error: [template-construction]

Alias = Template  # error: [template-construction]
type TypeAlias = Template  # error: [template-construction]
module = templatelib  # error: [template-construction]
dotted = string.templatelib.Template  # error: [template-construction]
new = Template.__new__  # error: [template-construction]
kind = type(t"a")  # error: [template-construction]
made = type(t"a")("x")  # error: [template-construction]
mapped = list(map(Template, ["a"]))  # error: [template-construction]
private = templatelib._template_unpickle  # error: [template-construction]  # ty: ignore[unresolved-attribute]
unpickle = _template_unpickle  # error: [template-construction]
via_string = string.templatelib  # error: [template-construction]

text = input()

# the subscripted value of a subscript is a runtime value
listed = [Template][0]("x")  # error: [template-construction]
tupled = (Template,)[0]("x")  # error: [template-construction]
mapped_class = {"a": Template}["a"]("x")  # error: [template-construction]
or_class = (Template or int)[0]  # error: [template-construction]  # ty: ignore[not-subscriptable]
starred = [*[Template]][0]("x")  # error: [template-construction]  # ruff: ignore[unnecessary-iterable-allocation-for-first-element]
attr_in_list = [string.templatelib.Template][0]("x")  # error: [template-construction]
module_in_list = [string.templatelib][0].Template("x")  # error: [template-construction]
nested = list[[Template][0]]  # error: [template-construction]  # ty: ignore[invalid-type-form]
casted = cast([Template][0], text)  # error: [template-construction]  # ruff: ignore[runtime-cast-value]  # ty: ignore[disjoint-cast]
checked = isinstance(text, [Template][0])  # error: [template-construction]
called = [Template][0](text)  # error: [template-construction]

# a private module of this package re-exports string.templatelib
rowx_alias = RowxTemplate(text)  # error: [template-construction]
rowx_attr = private_module.Template(text)  # error: [template-construction]
rowx_dotted = rowx._template.Template(text)  # error: [template-construction]
rowx_from = private_alias.Template(text)  # error: [template-construction]
rowx_interpolation = RowxInterpolation(1, "a", None, text)  # error: [template-construction]
rowx_module = private_module  # error: [template-construction]
rowx_class = RowxTemplate  # error: [template-construction]

# `Annotated[X, ...]` is callable at runtime and builds an `X`
annotated_call = Annotated[Template, 1](text)  # error: [template-construction]  # ty: ignore[call-non-callable]
annotated_attr = typing.Annotated[Template, 1](text)  # error: [template-construction]  # ty: ignore[call-non-callable]
annotated_alias = Annotated[Template, 1]  # error: [template-construction]
annotated_nested = Annotated[string.templatelib.Template, 1]  # error: [template-construction]
