import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from rowx_core._template import Compiled, Identifier, TemplatePlan, iter_parts

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

CONTROL_CHARACTER: Final[re.Pattern[str]] = re.compile(r"[\x00-\x1f\x7f-\x9f]")


# The parts of compilation that differ between backends.
#   placeholder: the text of the n-th parameter (1-based), such as `$1` or `?1`
#   pads_before_param: whether a character that ends the text before a parameter needs a space between them
#   pads_after_param: whether a character that starts the text after a parameter needs a space between them
#   quote_ident: quotes an identifier value (a str or a tuple of them)
#   ends_in_code: whether SQL text ends in plain code, not inside a string literal or a comment
#   max_parameters: a ceiling checked at compile time, `None` leaves the check to the backend at execute
@dataclass(slots=True, kw_only=True, frozen=True)
class Dialect:
    placeholder: Callable[[int], str]
    pads_before_param: Callable[[str], bool]
    pads_after_param: Callable[[str], bool]
    quote_ident: Callable[[object], str]
    ends_in_code: Callable[[str], bool]
    max_parameters: int | None = None


class Builder:
    def __init__(self, dialect: Dialect, /) -> None:
        self._dialect = dialect
        self._placeholder = dialect.placeholder
        self._pads_before = dialect.pads_before_param
        self._pads_after = dialect.pads_after_param
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
        # A space after a placeholder keeps "{y}x" from lexing as one token.
        if self._after_param and self._pads_after(text[0]):
            self._push(" ")
            self._leading = 1
        # A space after an identifier keeps `"a"` followed by `"b"` from lexing as one identifier.
        if self._after_ident and self._last == '"' and text[0] == '"':
            self._push(" ")
        self._after_param = False
        self._after_ident = False
        self._push(text)

    def param(self, value: object | None = None, *, keep: bool = True) -> None:
        # A space before a placeholder keeps "x{y}" from lexing as "x$1".
        trailing = int(bool(self._last) and self._pads_before(self._last))
        if trailing:
            self._push(" ")
        self.padding.append((self._leading, trailing))
        self._leading = 0
        self.segments.append((self._segment_start, self._len))
        text = self._placeholder(len(self.placeholders) + 1)
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
        if not self._dialect.ends_in_code("".join(self._sql[self._lexed :])):
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


def compile_parts(strings: Sequence[str], dialect: Dialect, /) -> Compiled:
    b = Builder(dialect)
    for i, s in enumerate(strings):
        if i:
            b.param(keep=False)
        b.static(s)
    return b.finish()


def compile_plan(plan: TemplatePlan, dialect: Dialect, /) -> tuple[Compiled, list[object]]:
    builder = Builder(dialect)
    ceiling = dialect.max_parameters
    for part in iter_parts(plan):
        if isinstance(part, str):
            builder.static(part)
        elif isinstance(part, Identifier):
            builder.ident(dialect.quote_ident(part.parts))
        else:
            if ceiling is not None and len(builder.values) >= ceiling:
                msg = f"a query takes at most {ceiling} parameters, got {len(builder.values) + 1}"
                raise TypeError(msg)
            builder.param(part.value)
    return builder.finish(), builder.values


def quote_part(name: object, max_bytes: int, /) -> str:
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
    if len(raw) > max_bytes:
        msg = f"identifier exceeds {max_bytes} bytes"
        raise TypeError(msg)
    if b"\x00" in raw:
        msg = "identifier must not contain a NUL character"
        raise TypeError(msg)
    if CONTROL_CHARACTER.search(raw.decode()):
        msg = "identifier must not contain a control character"
        raise TypeError(msg)
    return (b'"' + raw.replace(b'"', b'""') + b'"').decode()


def quote_ident(value: object, max_bytes: int, /) -> str:
    if isinstance(value, tuple):
        # tuple.__iter__ and not iter(): a tuple subclass can't override the iteration
        parts = list(tuple.__iter__(value))  # ruff: ignore[unnecessary-dunder-call]
        if not parts:
            msg = "identifier tuple must not be empty"
            raise TypeError(msg)
        return ".".join([quote_part(part, max_bytes) for part in parts])
    return quote_part(value, max_bytes)
