from dataclasses import dataclass
from string.templatelib import Interpolation, Template
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Sequence

IDENT_MAX_BYTES: Final[int] = 63


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


def _quote_part(name: object) -> str:
    if not isinstance(name, str):
        msg = f"identifier must be a str, got {type(name).__name__}"
        raise TypeError(msg)
    if not name:
        msg = "identifier must not be empty"
        raise TypeError(msg)
    if "\x00" in name:
        msg = "identifier must not contain a NUL character"
        raise TypeError(msg)
    if len(name.encode()) > IDENT_MAX_BYTES:
        msg = f"identifier exceeds {IDENT_MAX_BYTES} bytes: {name!r}"
        raise TypeError(msg)
    return '"' + name.replace('"', '""') + '"'


def _quote_ident(value: object) -> str:
    if isinstance(value, tuple):
        if not value:
            msg = "identifier tuple must not be empty"
            raise TypeError(msg)
        return ".".join(_quote_part(part) for part in value)
    return _quote_part(value)


def _walk(b: _Builder, t: Template, *, dynamic: bool) -> None:
    for i, s in enumerate(t.strings):
        if i:
            _interpolation(b, t.interpolations[i - 1], dynamic=dynamic)
        b.static(s)


def _interpolation(b: _Builder, item: Interpolation[object], *, dynamic: bool) -> None:
    value = item.value
    if item.conversion is not None:
        msg = f"conversion !{item.conversion} is not supported in {{{item.expression}}}"
        raise TypeError(msg)
    if dynamic and item.format_spec == "ident":
        b.static(_quote_ident(value))
        return
    if item.format_spec:
        msg = f"format spec {item.format_spec!r} is not supported in {{{item.expression}}}"
        raise TypeError(msg)
    if isinstance(value, Template):
        if not dynamic:
            msg = "nested templates are only supported by query_unchecked"
            raise TypeError(msg)
        _walk(b, value, dynamic=dynamic)
        return
    b.param(value)


def compile_template(t: Template, *, dynamic: bool) -> tuple[Compiled, list[object]]:
    if not isinstance(t, Template):
        msg = f"expected a t-string Template, got {type(t).__name__}"
        raise TypeError(msg)
    b = _Builder()
    _walk(b, t, dynamic=dynamic)
    return b.finish(), b.values
