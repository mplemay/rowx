import re
from typing import TYPE_CHECKING, Final

from rowx_core import Compiled, TemplatePlan, capture_template
from rowx_core._compiler import (  # ruff: ignore[import-private-name]
    Dialect,
    compile_parts as compile_parts_impl,
    compile_plan as compile_plan_impl,
    quote_ident as quote_ident_impl,
    quote_part as quote_part_impl,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from string.templatelib import Template

IDENT_MAX_BYTES: Final[int] = 63
MAX_PARAMETERS: Final[int] = 65535

IDENT_START_CLASS: Final[str] = r"A-Za-z_\x80-\U0010ffff"
# tokens that can change the lexical state; identifiers are consumed whole so that "$" or "E" inside
# one is never mistaken for a dollar-quote or an escape-string prefix
CODE_TOKEN: Final[re.Pattern[str]] = re.compile(
    rf"""'|"|--|/\*|\$(?:[{IDENT_START_CLASS}][{IDENT_START_CLASS}0-9]*)?\$|[{IDENT_START_CLASS}][{IDENT_START_CLASS}0-9$]*|[0-9]+""",
)
ESCAPE_STRING_TOKEN: Final[re.Pattern[str]] = re.compile(r"\\.|''|'", re.DOTALL)
# scan.l quotecontinue: whitespace with at least one newline (and `--` comments), then another quote, continues the
# same literal in the same mode. Possessive repetition keeps every step unambiguous, so a failed match is linear
# (scan.l: horiz_space is [ \t\f\v], space adds the two newline characters)
STRING_CONTINUATION: Final[re.Pattern[str]] = re.compile(
    r"(?:[ \t\f\v]|--[^\n\r]*+)*+[\n\r](?:[ \t\n\r\f\v]|--[^\n\r]*+[\n\r])*+'",
)
LINE_END: Final[re.Pattern[str]] = re.compile(r"[\r\n]")
BLOCK_TOKEN: Final[re.Pattern[str]] = re.compile(r"/\*|\*/")


def _is_ident_char(ch: str) -> bool:
    return (ch.isascii() and (ch.isalnum() or ch in "_$")) or not ch.isascii()


def _skip_quoted(text: str, i: int, quote: str) -> int:
    # a doubled quote is an escaped quote
    while (j := text.find(quote, i)) >= 0:
        if not text.startswith(quote, j + 1):
            return j + 1
        i = j + 2
    return -1


def _skip_escape_string(text: str, i: int) -> int:
    while m := ESCAPE_STRING_TOKEN.search(text, i):
        if m[0] == "'":
            return m.end()
        i = m.end()
    return -1


def _skip_string(text: str, i: int, *, escapes: bool) -> int:
    while (end := _skip_escape_string(text, i) if escapes else _skip_quoted(text, i, "'")) >= 0:
        if not (m := STRING_CONTINUATION.match(text, end)):
            return end
        i = m.end()
    return -1


def _skip_line_comment(text: str, i: int) -> int:
    m = LINE_END.search(text, i)
    return m.end() if m else -1


def _skip_block_comment(text: str, i: int) -> int:
    depth = 1
    while m := BLOCK_TOKEN.search(text, i):
        depth += 1 if m[0] == "/*" else -1
        i = m.end()
        if not depth:
            return i
    return -1


def _ends_in_code(text: str) -> bool:
    i = 0
    while m := CODE_TOKEN.search(text, i):
        token = m[0]
        i = m.end()
        match token[0]:
            case "'":
                i = _skip_string(text, i, escapes=False)
            case '"':
                i = _skip_quoted(text, i, '"')
            case "-":
                i = _skip_line_comment(text, i)
            case "/":
                i = _skip_block_comment(text, i)
            case "$":
                j = text.find(token, i)
                i = j + len(token) if j >= 0 else -1
            case _:
                # E'...' is the only string with backslash escapes; U&'', B'', X'' and N'' are ordinary strings
                if token in {"e", "E"} and text.startswith("'", i):
                    i = _skip_string(text, i + 1, escapes=True)
        if i < 0:
            return False
    return True


def _quote_part(name: object) -> str:
    return quote_part_impl(name, IDENT_MAX_BYTES)


def _quote_ident(value: object) -> str:
    return quote_ident_impl(value, IDENT_MAX_BYTES)


DIALECT: Final[Dialect] = Dialect(
    placeholder="${}".format,
    pads_before_param=_is_ident_char,
    pads_after_param=_is_ident_char,
    quote_ident=_quote_ident,
    ends_in_code=_ends_in_code,
    max_parameters=MAX_PARAMETERS,
)


def compile_parts(strings: Sequence[str]) -> Compiled:
    return compile_parts_impl(strings, DIALECT)


def compile_plan(plan: TemplatePlan) -> tuple[Compiled, list[object]]:
    return compile_plan_impl(plan, DIALECT)


def compile_template(template: Template, *, dynamic: bool) -> tuple[Compiled, list[object]]:
    return compile_plan(capture_template(template, dynamic=dynamic))


class PostgresCompiler:
    __slots__ = ()

    @staticmethod
    def compile(plan: TemplatePlan, /) -> tuple[Compiled, list[object]]:
        return compile_plan(plan)

    @staticmethod
    def compile_parts(strings: Sequence[str], /) -> Compiled:
        return compile_parts(strings)


COMPILER: Final[PostgresCompiler] = PostgresCompiler()
