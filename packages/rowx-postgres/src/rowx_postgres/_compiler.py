import re
from typing import TYPE_CHECKING, Final

from rowx_core import Compiled, Identifier, TemplatePlan, capture_template, iter_parts

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
CONTROL_CHARACTER: Final[re.Pattern[str]] = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _is_ident_char(ch: str) -> bool:
    return (ch.isascii() and (ch.isalnum() or ch in "_$")) or not ch.isascii()


class _Builder:
    def __init__(self) -> None:
        self._sql: list[str] = []
        self._len = 0
        self._last = ""
        self._after_param = False
        self._after_ident = False
        self._segment_start = 0
        # index into _sql of the first chunk that has not been lexed yet
        self._lexed = 0
        self.placeholders: list[tuple[int, int]] = []
        self.segments: list[tuple[int, int]] = []
        self.values: list[object] = []
        self.padding: list[tuple[int, int]] = []
        self._leading = 0

    def _push(self, text: str) -> None:
        self._sql.append(text)
        self._len += len(text)
        self._last = text[-1]

    def static(self, text: str) -> None:
        if not text:
            return
        # A space after "$n" keeps "{y}x" from lexing as one identifier.
        if self._after_param and _is_ident_char(text[0]):
            self._push(" ")
            self._leading = 1
        # A space after an identifier keeps `"a"` followed by `"b"` from lexing as one identifier.
        if self._after_ident and self._last == '"' and text[0] == '"':
            self._push(" ")
        self._after_param = False
        self._after_ident = False
        self._push(text)

    def param(self, value: object | None = None, *, keep: bool = True) -> None:
        # A space before "$n" keeps "x{y}" from lexing as "x$1".
        trailing = int(bool(self._last) and _is_ident_char(self._last))
        if trailing:
            self._push(" ")
        self.padding.append((self._leading, trailing))
        self._leading = 0
        self.segments.append((self._segment_start, self._len))
        n = len(self.placeholders) + 1
        text = f"${n}"
        start = self._len
        self._push(text)
        self.placeholders.append((start, self._len))
        self._segment_start = self._len
        self._after_param = True
        self._after_ident = False
        if keep:
            self.values.append(value)

    def ident(self, quoted: str) -> None:
        # Everything emitted since the previous ident is lexed as one string, which is the real SQL text. The scan
        # ends here, where the quoted identifier starts, and the quoted identifier is plain code.
        if not _ends_in_code("".join(self._sql[self._lexed :])):
            msg = "an ident interpolation must not be inside a string literal or comment"
            raise TypeError(msg)
        # A space before the identifier keeps it from fusing with a preceding quoted identifier.
        if self._last == '"':
            self._push(" ")
        self.static(quoted)
        self._after_ident = True
        self._lexed = len(self._sql)

    def finish(self) -> Compiled:
        self.segments.append((self._segment_start, self._len))
        self.padding.append((self._leading, 0))
        return Compiled("".join(self._sql), tuple(self.placeholders), tuple(self.segments), tuple(self.padding))


def compile_parts(strings: Sequence[str]) -> Compiled:
    b = _Builder()
    for i, s in enumerate(strings):
        if i:
            b.param(keep=False)
        b.static(s)
    return b.finish()


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
    if not isinstance(name, str):
        msg = f"identifier must be a str, got {type(name).__name__}"
        raise TypeError(msg)
    # the only call on name; everything below uses the encoded bytes and the exact str decoded from them
    try:
        raw = str.encode(name)
    except UnicodeEncodeError:
        msg = "identifier must be valid UTF-8 text"
        raise TypeError(msg) from None
    if not raw:
        msg = "identifier must not be empty"
        raise TypeError(msg)
    if len(raw) > IDENT_MAX_BYTES:
        msg = f"identifier exceeds {IDENT_MAX_BYTES} bytes"
        raise TypeError(msg)
    if b"\x00" in raw:
        msg = "identifier must not contain a NUL character"
        raise TypeError(msg)
    if CONTROL_CHARACTER.search(raw.decode()):
        msg = "identifier must not contain a control character"
        raise TypeError(msg)
    return (b'"' + raw.replace(b'"', b'""') + b'"').decode()


def _quote_ident(value: object) -> str:
    if isinstance(value, tuple):
        # tuple.__iter__ and not iter(): a tuple subclass can't override the iteration
        parts = list(tuple.__iter__(value))  # ruff: ignore[unnecessary-dunder-call]
        if not parts:
            msg = "identifier tuple must not be empty"
            raise TypeError(msg)
        return ".".join([_quote_part(part) for part in parts])
    return _quote_part(value)


def compile_plan(plan: TemplatePlan) -> tuple[Compiled, list[object]]:
    builder = _Builder()
    for part in iter_parts(plan):
        if isinstance(part, str):
            builder.static(part)
        elif isinstance(part, Identifier):
            builder.ident(_quote_ident(part.parts))
        else:
            if len(builder.values) >= MAX_PARAMETERS:
                msg = f"a query takes at most {MAX_PARAMETERS} parameters, got {len(builder.values) + 1}"
                raise TypeError(msg)
            builder.param(part.value)
    return builder.finish(), builder.values


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
