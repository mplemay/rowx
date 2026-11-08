from dataclasses import dataclass
from re import Pattern, compile as re_compile
from string.templatelib import Interpolation, Template
from typing import TYPE_CHECKING, Final, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

CONTROL_CHARACTER: Final[Pattern[str]] = re_compile(r"[\x00-\x1f\x7f-\x9f]")


@dataclass(frozen=True, slots=True, kw_only=True)
class Parameter:
    value: object


@dataclass(frozen=True, slots=True, kw_only=True)
class Identifier:
    parts: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class TemplatePlan:
    parts: tuple[str | Parameter | Identifier | TemplatePlan, ...]


@dataclass(frozen=True, slots=True)
class Compiled:
    sql: str
    placeholders: tuple[tuple[int, int], ...]
    segments: tuple[tuple[int, int], ...]
    padding: tuple[tuple[int, int], ...]


class Compiler(Protocol):
    def compile(self, plan: TemplatePlan, /) -> tuple[Compiled, list[object]]: ...
    def compile_parts(self, strings: Sequence[str], /) -> Compiled: ...


def _exact_str(value: object) -> str | None:
    return value if type(value) is str else None


def _identifier(value: object) -> Identifier:
    if isinstance(value, tuple):
        # Bypass subclass hooks when capturing the identifier's immutable value.
        values = list(tuple.__iter__(value))  # ruff: ignore[unnecessary-dunder-call]
        if not values:
            msg = "identifier tuple must not be empty"
            raise TypeError(msg)
    else:
        values = [value]
    parts: list[str] = []
    for part in values:
        if not isinstance(part, str):
            msg = f"identifier must be a str, got {type(part).__name__}"
            raise TypeError(msg)
        try:
            raw = str.encode(part)
        except UnicodeEncodeError:
            msg = "identifier must be valid UTF-8 text"
            raise TypeError(msg) from None
        if not raw:
            msg = "identifier must not be empty"
            raise TypeError(msg)
        text = raw.decode()
        if "\x00" in text:
            msg = "identifier must not contain a NUL character"
            raise TypeError(msg)
        if CONTROL_CHARACTER.search(text):
            msg = "identifier must not contain a control character"
            raise TypeError(msg)
        parts.append(text)
    return Identifier(parts=tuple(parts))


def _interpolation(
    item: Interpolation[object], *, dynamic: bool, memo: dict[int, TemplatePlan]
) -> Parameter | Identifier | TemplatePlan:
    value = item.value
    # Interpolation metadata is untrusted, including str subclasses with custom hooks.
    expression = _exact_str(item.expression) or "?"
    if item.conversion is not None:
        if (conversion := _exact_str(item.conversion)) is None:
            msg = f"conversion must be a str or None in {{{expression}}}"
        else:
            msg = f"conversion !{conversion} is not supported in {{{expression}}}"
        raise TypeError(msg)
    if (spec := _exact_str(item.format_spec)) is None:
        msg = f"format spec must be a str in {{{expression}}}"
        raise TypeError(msg)
    if dynamic and spec == "ident":
        return _identifier(value)
    if spec:
        msg = f"format spec {spec!r} is not supported in {{{expression}}}"
        raise TypeError(msg)
    if type(value) is Template:
        if not dynamic:
            msg = "nested templates are only supported by query_unchecked"
            raise TypeError(msg)
        return _capture(value, dynamic=dynamic, memo=memo)
    return Parameter(value=value)


def _capture(template: Template, *, dynamic: bool, memo: dict[int, TemplatePlan]) -> TemplatePlan:
    if (cached := memo.get(id(template))) is not None:
        return cached
    parts: list[str | Parameter | Identifier | TemplatePlan] = []
    for index, text in enumerate(template.strings):
        if index:
            part = _interpolation(template.interpolations[index - 1], dynamic=dynamic, memo=memo)
            if not isinstance(part, TemplatePlan) or part.parts:
                parts.append(part)
        exact = str.join("", (text,))
        if "\x00" in exact:
            msg = "SQL text must not contain a NUL character"
            raise TypeError(msg)
        if exact:
            parts.append(exact)
    plan = TemplatePlan(parts=tuple(parts))
    memo[id(template)] = plan
    return plan


def capture_template(template: Template, *, dynamic: bool) -> TemplatePlan:
    if type(template) is not Template:
        msg = f"expected a t-string Template, got {type(template).__name__}"
        raise TypeError(msg)
    # Keep shared nested templates as a DAG; expansion and dialect limits belong to compilation.
    return _capture(template, dynamic=dynamic, memo={})


def iter_parts(plan: TemplatePlan) -> Iterator[str | Parameter | Identifier]:
    pending = [iter(plan.parts)]
    while pending:
        if (part := next(pending[-1], None)) is None:
            pending.pop()
        elif isinstance(part, TemplatePlan):
            pending.append(iter(part.parts))
        else:
            yield part
