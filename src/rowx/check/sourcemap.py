import ast
from contextlib import suppress
from dataclasses import dataclass
from io import StringIO
from re import compile as re_compile
from tokenize import (
    OP,
    TSTRING_END,
    TSTRING_MIDDLE,
    TSTRING_START,
    TokenError,
    generate_tokens,
)
from typing import TYPE_CHECKING, Final
from unicodedata import lookup

from rowx._template import _is_ident_char as is_ident_char, compile_parts
from rowx.check.model import Span

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from tokenize import TokenInfo

LINE_PATTERN: Final = re_compile(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+")
SIMPLE_ESCAPES: Final[dict[str, str]] = {
    "\\": "\\",
    "'": "'",
    '"': '"',
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
}
HEX_ESCAPE_WIDTHS: Final[dict[str, int]] = {"x": 2, "u": 4, "U": 8}
HEX_DIGITS: Final[frozenset[str]] = frozenset("0123456789abcdefABCDEF")
OCTAL_DIGITS: Final[frozenset[str]] = frozenset("01234567")
OCTAL_MAX_DIGITS: Final[int] = 3
SPACE: Final[str] = " "

type Pos = tuple[int, int]


@dataclass(slots=True, frozen=True)
class SourceMap:
    sql: str
    # one span per character of `sql`
    spans: tuple[Span, ...]
    # where an error at the end of input lands
    end: Span
    # one flag per static segment, false when the decoded source text did not match the AST
    exact: tuple[bool, ...]

    def locate(self, position: int) -> Span:
        # the server's `position` is a 1-based character index into the SQL
        index = position - 1
        if 0 <= index < len(self.spans):
            return self.spans[index]
        return self.end


@dataclass(slots=True)
class _Char:
    char: str
    start: Pos
    end: Pos


@dataclass(slots=True)
class _Segment:
    chars: list[_Char]
    start: Pos | None = None
    end: Pos | None = None


class SourceMapper:
    def __init__(self, source: str) -> None:
        self._lines = [line.encode() for line in LINE_PATTERN.findall(source)]
        self._source = source
        self._tokens: list[TokenInfo] | None = None

    def _col(self, line: int, byte_col: int) -> int:
        if not 1 <= line <= len(self._lines):
            return byte_col
        return len(self._lines[line - 1][:byte_col].decode(errors="replace"))

    def _tokenize(self) -> list[TokenInfo]:
        if self._tokens is None:
            tokens: list[TokenInfo] = []
            with suppress(TokenError, SyntaxError, IndentationError):
                tokens.extend(generate_tokens(StringIO(self._source, newline="").readline))
            self._tokens = tokens
        return self._tokens

    def build(self, node: ast.TemplateStr) -> SourceMap:
        start = (node.lineno, self._col(node.lineno, node.col_offset))
        end_line = node.end_lineno or node.lineno
        end = (end_line, self._col(end_line, node.end_col_offset or 0))
        segments, holes = _collect(t for t in self._tokenize() if start <= t.start < end)
        expected = _constants(node)
        if len(segments) != len(expected) or len(holes) != len(expected) - 1:
            # the token stream disagrees with the AST, so no position can be trusted
            segments = [_Segment([], start=start, end=end) for _ in expected]
            holes = [_span(start, end)] * (len(expected) - 1)
        return _assemble(expected, segments, holes)


def _constants(node: ast.TemplateStr) -> list[str]:
    segments = [""]
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            segments[-1] += value.value
        else:
            segments.append("")
    return segments


def _span(start: Pos, end: Pos) -> Span:
    # tokenize columns are 0-based code point offsets
    return Span(line=start[0], col=start[1] + 1, end_line=end[0], end_col=end[1] + 1)


def _advance(pos: Pos, char: str) -> Pos:
    if char == "\n":
        return (pos[0] + 1, 0)
    return (pos[0], pos[1] + 1)


class _Collector:
    def __init__(self) -> None:
        self.segments = [_Segment([])]
        self.holes: list[Span] = []
        self._depth = 0
        self._raw = False
        self._hole_start: Pos = (0, 0)

    def _brace(self, token: TokenInfo) -> None:
        current = self.segments[-1]
        if token.string == "{":
            if self._depth == 0:
                current.end = token.start
                self._hole_start = token.start
            self._depth += 1
            return
        self._depth -= 1
        if self._depth == 0:
            self.holes.append(_span(self._hole_start, token.end))
            self.segments.append(_Segment([], start=token.end))

    def _text(self, token: TokenInfo) -> None:
        current = self.segments[-1]
        if token.type == TSTRING_START:
            self._raw = "r" in token.string.lower()
            if current.start is None:
                current.start = token.end
        elif token.type == TSTRING_MIDDLE:
            current.chars.extend(_decode(token.string, token.start, raw=self._raw))
        elif token.type == TSTRING_END:
            current.end = token.start

    def feed(self, token: TokenInfo) -> None:
        if token.type == OP and token.string in "{}":
            self._brace(token)
        elif self._depth == 0:
            self._text(token)


def _collect(tokens: Iterable[TokenInfo]) -> tuple[list[_Segment], list[Span]]:
    collector = _Collector()
    for token in tokens:
        collector.feed(token)
    return collector.segments, collector.holes


def _decode(text: str, start: Pos, *, raw: bool) -> list[_Char]:
    chars: list[_Char] = []
    pos = start
    i = 0
    n = len(text)

    def emit(char: str, length: int) -> None:
        nonlocal pos, i
        end = pos
        for consumed in text[i : i + length]:
            end = _advance(end, consumed)
        chars.append(_Char(char, pos, end))
        pos = end
        i += length

    def skip(length: int) -> None:
        nonlocal pos, i
        for consumed in text[i : i + length]:
            pos = _advance(pos, consumed)
        i += length

    while i < n:
        ch = text[i]
        if ch == "\r":
            # \r\n and a lone \r both read as one \n
            emit("\n", 2 if text[i + 1 : i + 2] == "\n" else 1)
        elif ch != "\\" or raw:
            emit(ch, 1)
        elif i + 1 >= n:
            emit("\\", 1)
        else:
            _escape(text, i, emit, skip)
    return chars


def _escape(text: str, i: int, emit: Callable[[str, int], None], skip: Callable[[int], None]) -> None:
    nxt = text[i + 1]
    if nxt == "\n":
        skip(2)
    elif nxt == "\r":
        skip(3 if text[i + 2 : i + 3] == "\n" else 2)
    elif nxt in SIMPLE_ESCAPES:
        emit(SIMPLE_ESCAPES[nxt], 2)
    elif nxt in OCTAL_DIGITS:
        digits = nxt
        while len(digits) < OCTAL_MAX_DIGITS and text[i + 1 + len(digits) : i + 2 + len(digits)] in OCTAL_DIGITS - {""}:
            digits += text[i + 1 + len(digits)]
        emit(chr(int(digits, 8)), 1 + len(digits))
    elif nxt in HEX_ESCAPE_WIDTHS:
        width = HEX_ESCAPE_WIDTHS[nxt]
        digits = text[i + 2 : i + 2 + width]
        if len(digits) == width and set(digits) <= HEX_DIGITS and int(digits, 16) <= 0x10FFFF:  # noqa: PLR2004
            emit(chr(int(digits, 16)), 2 + width)
        else:
            emit("\\", 1)
    elif nxt == "N" and text[i + 2 : i + 3] == "{" and (close := text.find("}", i + 3)) != -1:
        try:
            emit(lookup(text[i + 3 : close]), close + 1 - i)
        except KeyError:
            emit("\\", 1)
    else:
        # an unknown escape keeps its backslash and the next character, as two characters
        emit("\\", 1)


def _fill(
    spans: list[Span | None],
    sql_range: tuple[int, int],
    text: str,
    segment: _Segment,
    pads: tuple[int, Span | None, Span | None],
) -> bool:
    begin, finish = sql_range
    lead, before, after = pads
    ok = "".join(c.char for c in segment.chars) == text and segment.start is not None
    fallback = _span(segment.start, segment.start) if segment.start is not None else None
    for offset in range(finish - begin):
        char_index = offset - lead
        if 0 <= char_index < len(text):
            c = segment.chars[char_index] if ok else None
            spans[begin + offset] = _span(c.start, c.end) if c else fallback
        elif char_index < 0:
            # a space after `{x}` maps back to that placeholder
            spans[begin + offset] = before
        else:
            # a space before `{x}` maps to that placeholder
            spans[begin + offset] = after
    return ok


def _assemble(expected: list[str], segments: list[_Segment], holes: list[Span]) -> SourceMap:
    compiled = compile_parts(expected)
    sql = compiled.sql
    spans: list[Span | None] = [None] * len(sql)
    exact: list[bool] = []
    for index, (text, segment) in enumerate(zip(expected, segments, strict=True)):
        # the adjacency rule may pad a segment with a space on either side
        lead = int(index > 0 and bool(text) and is_ident_char(text[0]))
        before = holes[index - 1] if index > 0 else None
        after = holes[index] if index < len(holes) else None
        exact.append(_fill(spans, compiled.segments[index], text, segment, (lead, before, after)))
    for index, (begin, finish) in enumerate(compiled.placeholders):
        for slot in range(begin, finish):
            spans[slot] = holes[index]
    last = segments[-1]
    if last.chars and exact[-1]:
        tail = last.chars[-1].end
    elif last.end is not None:
        tail = last.end
    else:
        tail = last.start or (1, 0)
    resolved = tuple(s if s is not None else _span(tail, tail) for s in spans)
    return SourceMap(sql=sql, spans=resolved, end=_span(tail, tail), exact=tuple(exact))
