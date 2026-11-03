import re
from dataclasses import dataclass
from string.templatelib import Interpolation, Template
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Sequence

IDENT_MAX_BYTES: Final[int] = 63
MAX_PARAMETERS: Final[int] = 65535

IDENT_START_CLASS: Final[str] = r"A-Za-z_\x80-\U0010ffff"
# tokens that can change the lexical state; identifiers are consumed whole so that "$" or "E" inside
# one is never mistaken for a dollar-quote or an escape-string prefix
CODE_TOKEN: Final[re.Pattern[str]] = re.compile(
    rf"""'|"|--|/\*|\$(?:[{IDENT_START_CLASS}][{IDENT_START_CLASS}0-9]*)?\$|[{IDENT_START_CLASS}][{IDENT_START_CLASS}0-9$]*|[0-9]+""",
)
ESCAPE_STRING_TOKEN: Final[re.Pattern[str]] = re.compile(r"\\.|''|'", re.DOTALL)
LINE_END: Final[re.Pattern[str]] = re.compile(r"[\r\n]")
BLOCK_TOKEN: Final[re.Pattern[str]] = re.compile(r"/\*|\*/")
CONTROL_CHARACTER: Final[re.Pattern[str]] = re.compile(r"[\x00-\x1f\x7f-\x9f]")


@dataclass(frozen=True, slots=True)
class Compiled:
    sql: str
    placeholders: tuple[tuple[int, int], ...]
    segments: tuple[tuple[int, int], ...]


def _is_ident_char(ch: str) -> bool:
    return (ch.isascii() and (ch.isalnum() or ch in "_$")) or not ch.isascii()


class _Builder:
    def __init__(self) -> None:
        self._sql: list[str] = []
        self._len = 0
        self._last = ""
        self._after_param = False
        self._segment_start = 0
        # index into _sql of the first chunk that has not been lexed yet
        self._lexed = 0
        self.placeholders: list[tuple[int, int]] = []
        self.segments: list[tuple[int, int]] = []
        self.values: list[object] = []

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
        self._after_param = False
        self._push(text)

    def param(self, value: object | None = None, *, keep: bool = True) -> None:
        # A space before "$n" keeps "x{y}" from lexing as "x$1".
        if self._last and _is_ident_char(self._last):
            self._push(" ")
        self.segments.append((self._segment_start, self._len))
        n = len(self.placeholders) + 1
        text = f"${n}"
        start = self._len
        self._push(text)
        self.placeholders.append((start, self._len))
        self._segment_start = self._len
        self._after_param = True
        if keep:
            self.values.append(value)

    def ident(self, quoted: str) -> None:
        # Everything emitted since the previous ident is lexed as one string, which is the real SQL text. The scan
        # ends here, where the quoted identifier starts, and the quoted identifier is plain code.
        if not _ends_in_code("".join(self._sql[self._lexed :])):
            msg = "an ident interpolation must not be inside a string literal or comment"
            raise TypeError(msg)
        self.static(quoted)
        self._lexed = len(self._sql)

    def finish(self) -> Compiled:
        self.segments.append((self._segment_start, self._len))
        return Compiled("".join(self._sql), tuple(self.placeholders), tuple(self.segments))


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
                i = _skip_quoted(text, i, "'")
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
                    i = _skip_escape_string(text, i + 1)
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


def _walk(b: _Builder, t: Template, *, dynamic: bool) -> None:
    for i, s in enumerate(t.strings):
        if i:
            _interpolation(b, t.interpolations[i - 1], dynamic=dynamic)
        b.static(s)


def _exact_str(value: object) -> str | None:
    return value if type(value) is str else None


def _interpolation(b: _Builder, item: Interpolation[object], *, dynamic: bool) -> None:
    value = item.value
    # conversion, format_spec and expression are untrusted like value: only an exact str is ever examined
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
        b.ident(_quote_ident(value))
        return
    if spec:
        msg = f"format spec {spec!r} is not supported in {{{expression}}}"
        raise TypeError(msg)
    if type(value) is Template:
        if not dynamic:
            msg = "nested templates are only supported by query_unchecked"
            raise TypeError(msg)
        _walk(b, value, dynamic=dynamic)
        return
    b.param(value)


def compile_template(t: Template, *, dynamic: bool) -> tuple[Compiled, list[object]]:
    if type(t) is not Template:
        msg = f"expected a t-string Template, got {type(t).__name__}"
        raise TypeError(msg)
    b = _Builder()
    _walk(b, t, dynamic=dynamic)
    compiled = b.finish()
    if "\x00" in compiled.sql:
        msg = "SQL text must not contain a NUL character"
        raise TypeError(msg)
    if len(b.values) > MAX_PARAMETERS:
        msg = f"a query takes at most {MAX_PARAMETERS} parameters, got {len(b.values)}"
        raise TypeError(msg)
    return compiled, b.values
