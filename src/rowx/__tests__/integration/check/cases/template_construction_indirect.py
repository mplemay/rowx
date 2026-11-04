import string.templatelib
from string import templatelib
from string.templatelib import Template, _template_unpickle  # error: [template-construction]  # ty: ignore[unresolved-import]

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
