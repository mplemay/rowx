import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal

from rowx_core import Compiled, TemplatePlan, capture_template
from rowx_core._compiler import (  # ruff: ignore[import-private-name]
    Dialect,
    compile_parts as compile_parts_impl,
    compile_plan as compile_plan_impl,
    quote_ident as quote_ident_impl,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from string.templatelib import Template

# SQLite has no identifier length limit of its own; this is SQLITE_MAX_LENGTH, the longest any SQL text can be
IDENT_MAX_BYTES: Final[int] = 1_000_000_000

IDENT_START_CLASS: Final[str] = r"A-Za-z_\x80-\U0010ffff"
IDENT_CLASS: Final[str] = r"A-Za-z_0-9$\x80-\U0010ffff"
# tokenize.c: anything that is not whitespace, a quote, a comment or one of the tokens below is a one-character token
SCAN_TOKEN: Final[re.Pattern[str]] = re.compile(
    rf"""(?P<ws>[ \t\n\f\r]+)
    |(?P<word>[{IDENT_START_CLASS}][{IDENT_CLASS}]*)
    |(?P<number>(?:[0-9]|\.[0-9])[{IDENT_CLASS}.]*)
    |(?P<query>\?[0-9]*)
    |(?P<sigil>[:@$\#])
    |(?P<quote>['"`\[])
    |(?P<comment>--|/\*)
    |(?P<punct>.)""",
    re.VERBOSE | re.DOTALL,
)
IDENT_CHAR: Final[re.Pattern[str]] = re.compile(rf"[{IDENT_CLASS}]")
SQL_SPACE: Final[str] = " \t\n\f\r"

type TokenKind = Literal["word", "quoted", "string", "number", "param", "punct"]


@dataclass(slots=True, kw_only=True, frozen=True)
class Token:
    kind: TokenKind
    start: int
    end: int
    # words and quoted identifiers are folded the way SQLite compares them (ASCII only); strings are unquoted
    value: str


FOLD_TABLE: Final[dict[int, int]] = {c: c + 32 for c in range(ord("A"), ord("Z") + 1)}


@dataclass(slots=True, kw_only=True, frozen=True)
class Lexed:
    tokens: tuple[Token, ...]
    # False when the text ends inside a string, quoted identifier or comment
    closed: bool


def _is_ident_char(ch: str) -> bool:
    return IDENT_CHAR.fullmatch(ch) is not None


def fold(name: str) -> str:
    # SQLite folds ASCII letters only
    return name.translate(FOLD_TABLE)


def _skip_quoted(text: str, i: int, quote: str) -> int:
    # a doubled quote is an escaped quote
    while (j := text.find(quote, i)) >= 0:
        if not text.startswith(quote, j + 1):
            return j + 1
        i = j + 2
    return -1


def _skip_variable(text: str, i: int) -> int:
    # tokenize.c CC_VARALPHA / CC_DOLLAR, including the Tcl `$a::b` and `$a(b)` forms
    n = 0
    j = i + 1
    size = len(text)
    while j < size:
        c = text[j]
        if IDENT_CHAR.match(c):
            n += 1
            j += 1
        elif c == "(" and n:
            j += 1
            while j < size and text[j] not in SQL_SPACE and text[j] != ")":
                j += 1
            return j + 1 if j < size and text[j] == ")" else j
        elif c == ":" and text.startswith(":", j + 1):
            j += 2
        else:
            break
    return j


def _quoted_token(text: str, start: int) -> Token | None:
    quote = text[start]
    if quote == "[":
        # no escape: the first `]` ends it
        close = text.find("]", start + 1)
        end = close + 1 if close >= 0 else -1
    else:
        end = _skip_quoted(text, start + 1, quote)
    if end < 0:
        return None
    body = text[start + 1 : end - 1]
    if quote == "'":
        return Token(kind="string", start=start, end=end, value=body.replace("''", "'"))
    if quote != "[":
        body = body.replace(quote * 2, quote)
    return Token(kind="quoted", start=start, end=end, value=fold(body))


# the index after the comment, or -1 when a line comment has no newline or a block comment is not closed
def _skip_comment(text: str, i: int, opener: str) -> int:
    if opener == "--":
        j = text.find("\n", i)
        return j + 1 if j >= 0 else -1
    j = text.find("*/", i)
    return j + 2 if j >= 0 else -1


def _simple_token(text: str, m: re.Match[str]) -> Token:
    kind = m.lastgroup
    start, end = m.span()
    if kind == "word":
        return Token(kind="word", start=start, end=end, value=fold(m[0]))
    if kind == "number":
        return Token(kind="number", start=start, end=end, value=m[0])
    if kind == "query":
        return Token(kind="param", start=start, end=end, value=m[0])
    if kind == "sigil":
        end = _skip_variable(text, start)
        return Token(kind="param", start=start, end=end, value=text[start:end])
    return Token(kind="punct", start=start, end=end, value=m[0])


def tokenize(text: str) -> Lexed:
    tokens: list[Token] = []
    i = 0
    while m := SCAN_TOKEN.match(text, i):
        kind = m.lastgroup
        i = m.end()
        if kind == "ws":
            continue
        if kind == "comment":
            i = _skip_comment(text, i, m[0])
            if i < 0:
                return Lexed(tokens=tuple(tokens), closed=False)
            continue
        if kind == "quote":
            if (quoted := _quoted_token(text, m.start())) is None:
                return Lexed(tokens=tuple(tokens), closed=False)
            tokens.append(quoted)
            i = quoted.end
            continue
        tokens.append(_simple_token(text, m))
        i = tokens[-1].end
    return Lexed(tokens=tuple(tokens), closed=True)


def _ends_in_code(text: str) -> bool:
    return tokenize(text).closed


def parameter_spans(sql: str) -> tuple[tuple[int, int], ...]:
    return tuple((t.start, t.end) for t in tokenize(sql).tokens if t.kind == "param")


def _check_parameters(compiled: Compiled) -> Compiled:
    sql = compiled.sql
    spans = parameter_spans(sql)
    known = set(compiled.placeholders)
    for start, end in spans:
        if (start, end) not in known:
            msg = f"{sql[start:end]!r} in static SQL is a parameter in SQLite; use an interpolation ({{value}}) instead"
            raise TypeError(msg)
    if len(spans) != len(known):
        msg = "an interpolation must not be inside a string literal, quoted identifier or comment"
        raise TypeError(msg)
    return compiled


def _quote_ident(value: object) -> str:
    return quote_ident_impl(value, IDENT_MAX_BYTES)


DIALECT: Final[Dialect] = Dialect(
    placeholder="?{}".format,
    pads_before_param=_is_ident_char,
    pads_after_param=_is_ident_char,
    quote_ident=_quote_ident,
    ends_in_code=_ends_in_code,
    max_parameters=None,
)


def compile_parts(strings: Sequence[str]) -> Compiled:
    return _check_parameters(compile_parts_impl(strings, DIALECT))


def compile_plan(plan: TemplatePlan) -> tuple[Compiled, list[object]]:
    compiled, values = compile_plan_impl(plan, DIALECT)
    return _check_parameters(compiled), values


def compile_template(template: Template, *, dynamic: bool) -> tuple[Compiled, list[object]]:
    return compile_plan(capture_template(template, dynamic=dynamic))


@dataclass(slots=True, kw_only=True, frozen=True)
class StatementFacts:
    # table-like names in FROM and JOIN position (including table-valued functions), each as its folded parts
    sources: tuple[tuple[str, ...], ...]
    cte_names: frozenset[str]
    has_subquery: bool
    has_compound: bool
    # LEFT, RIGHT, FULL, OUTER and NATURAL joins
    has_outer_join: bool


JOIN_WORDS: Final[frozenset[str]] = frozenset({"natural", "left", "right", "full", "outer", "inner", "cross"})
OUTER_WORDS: Final[frozenset[str]] = frozenset({"natural", "left", "right", "full", "outer"})
FROM_ENDS: Final[frozenset[str]] = frozenset(
    {"where", "group", "having", "window", "order", "limit", "union", "intersect", "except", "returning"},
)
COMPOUND_WORDS: Final[frozenset[str]] = frozenset({"union", "intersect", "except"})
SUBQUERY_STARTS: Final[frozenset[str]] = frozenset({"select", "with", "values"})
NAME_KINDS: Final[frozenset[TokenKind]] = frozenset({"word", "quoted", "string"})
CONFLICT_WORDS: Final[frozenset[str]] = frozenset({"rollback", "abort", "replace", "fail", "ignore"})
# words that follow UPDATE without being its target: DO UPDATE SET, BEFORE UPDATE OF/ON, ON UPDATE CASCADE/SET/NO/RESTRICT
UPDATE_NOT_TARGETS: Final[frozenset[str]] = frozenset({"set", "of", "on", "cascade", "no", "restrict", "from"})
# SQLite accepts `with` and `window` as identifiers; every other FROM-ending word is reserved
NOT_CTE_NAMES: Final[frozenset[str]] = (FROM_ENDS - {"window"}) | {"from", "as"}


type CteState = Literal["name", "after_name", "as", "after_body"]


@dataclass(slots=True)
class _Frame:
    kind: Literal["plain", "cte_body"] = "plain"
    from_active: bool = False
    expect_table: bool = False
    # None, or where the WITH clause header is
    cte: CteState | None = None
    join_words: set[str] = field(default_factory=set)


def _name_part(tok: Token) -> str:
    # a string used as a name compares folded, like a word or quoted identifier
    return fold(tok.value) if tok.kind == "string" else tok.value


def _name_parts(tokens: tuple[Token, ...], i: int) -> tuple[tuple[str, ...], int]:
    parts = [_name_part(tokens[i])]
    i += 1
    while (
        i + 1 < len(tokens)
        and tokens[i].value == "."
        and tokens[i].kind == "punct"
        and tokens[i + 1].kind in NAME_KINDS
    ):
        parts.append(_name_part(tokens[i + 1]))
        i += 2
    return tuple(parts), i


def _is_punct(tok: Token, value: str) -> bool:
    return tok.kind == "punct" and tok.value == value


class _Walker:
    def __init__(self, tokens: tuple[Token, ...]) -> None:
        self.tokens: tuple[Token, ...] = tokens
        # `_step` and its helpers run after `i` has moved past the current token, so the current one is `i - 1`
        self.i: int = 0
        self.stack: list[_Frame] = [_Frame()]
        self.sources: list[tuple[str, ...]] = []
        self.ctes: set[str] = set()
        self.subquery: bool = False
        self.compound: bool = False
        self.outer: bool = False

    def run(self) -> StatementFacts:
        while self.i < len(self.tokens):
            tok = self.tokens[self.i]
            self.i += 1
            self._step(tok)
        return StatementFacts(
            sources=tuple(self.sources),
            cte_names=frozenset(self.ctes),
            has_subquery=self.subquery,
            has_compound=self.compound,
            has_outer_join=self.outer,
        )

    def _step(self, tok: Token) -> None:
        f = self.stack[-1]
        if _is_punct(tok, ")"):
            self._close(f)
        elif f.cte is not None and self._cte(f, tok):
            return
        elif _is_punct(tok, "("):
            self._open(f)
        elif f.expect_table and tok.kind in NAME_KINDS:
            self._source()
        elif _is_punct(tok, ","):
            f.expect_table = f.expect_table or f.from_active
            f.join_words.clear()
        elif _is_punct(tok, ";"):
            self.stack = [_Frame()]
        elif tok.kind != "word":
            f.join_words.clear()
        else:
            self._word(f, tok.value)

    def _close(self, f: _Frame) -> None:
        if len(self.stack) > 1:
            self.stack.pop()
            if f.kind == "cte_body":
                self.stack[-1].cte = "after_body"

    # handles a token while a WITH header is being read; False means the token is not part of it
    def _cte(self, f: _Frame, tok: Token) -> bool:
        state, f.cte = f.cte, None
        word = tok.value if tok.kind == "word" else None
        if (state == "after_body" and _is_punct(tok, ",")) or (state == "name" and word == "recursive"):
            f.cte = "name"
        elif state == "name" and tok.kind in {"word", "quoted"}:
            self.ctes.add(tok.value)
            f.cte = "after_name"
        elif state == "after_name" and word == "as":
            f.cte = "as"
        elif state == "after_name" and _is_punct(tok, "("):
            f.cte = "after_name"
            self.stack.append(_Frame())
        elif state == "as" and word in {"not", "materialized"}:
            f.cte = "as"
        elif state == "as" and _is_punct(tok, "("):
            self.subquery = True
            self.stack.append(_Frame(kind="cte_body"))
        else:
            return False
        return True

    def _open(self, f: _Frame) -> None:
        nxt = self.tokens[self.i] if self.i < len(self.tokens) else None
        starts_query = nxt is not None and nxt.kind == "word" and nxt.value in SUBQUERY_STARTS
        self.subquery = self.subquery or starts_query
        # a parenthesised join group continues the FROM clause
        group = f.expect_table and not starts_query
        f.expect_table = False
        f.join_words.clear()
        self.stack.append(_Frame(from_active=group, expect_table=group))

    def _at(self, index: int) -> Token | None:
        return self.tokens[index] if 0 <= index < len(self.tokens) else None

    def _prev(self) -> Token | None:
        return self._at(self.i - 2)

    def _record(self, start: int) -> None:
        parts, self.i = _name_parts(self.tokens, start)
        self.sources.append(parts)

    def _source(self) -> None:
        f = self.stack[-1]
        self._record(self.i - 1)
        f.expect_table = False
        f.join_words.clear()

    # IS [NOT] DISTINCT FROM is an operator, not a FROM clause
    def _after_distinct(self) -> bool:
        prev = self._prev()
        return prev is not None and prev.kind == "word" and prev.value == "distinct"

    # `with` is also a valid column name or alias, so outside the start of a statement it must look like a CTE header
    def _starts_with_clause(self) -> bool:
        prev = self._prev()
        if prev is None or _is_punct(prev, ";"):
            return True
        if _is_punct(prev, "."):
            return False
        # covers `(WITH ...`, `AS WITH ...` (CREATE VIEW or TABLE) and `INSERT INTO t WITH ...`
        j = self.i
        if (tok := self._at(j)) is not None and tok.kind == "word" and tok.value == "recursive":
            j += 1
        name, after = self._at(j), self._at(j + 1)
        if name is None or after is None:
            return False
        named = name.kind == "quoted" or (name.kind == "word" and name.value not in NOT_CTE_NAMES)
        return named and (_is_punct(after, "(") or (after.kind == "word" and after.value == "as"))

    # a word from FROM_ENDS closes the FROM clause only as the clause keyword, not as an alias or qualified column
    def _ends_from(self, word: str) -> bool:
        prev = self._prev()
        if prev is not None and (_is_punct(prev, ".") or (prev.kind == "word" and prev.value == "as")):
            return False
        if word != "window":
            return True
        # a WINDOW clause is `WINDOW <name> AS (...)`
        name, after = self._at(self.i), self._at(self.i + 1)
        return (
            name is not None
            and name.kind in {"word", "quoted"}
            and after is not None
            and after.kind == "word"
            and after.value == "as"
        )

    # `x IN name`, `x IN schema.name` and `x IN fn(...)` read a table, view or table-valued function
    def _in_target(self) -> None:
        if (nxt := self._at(self.i)) is not None and nxt.kind in NAME_KINDS:
            self._record(self.i)
            self.subquery = True

    # UPDATE [OR <conflict>] <target>
    def _update_target(self) -> None:
        prev = self._prev()
        if prev is not None and _is_punct(prev, "."):
            return
        j = self.i
        or_tok, conflict = self._at(j), self._at(j + 1)
        if (
            or_tok is not None
            and or_tok.kind == "word"
            and or_tok.value == "or"
            and conflict is not None
            and conflict.value in CONFLICT_WORDS
        ):
            j += 2
        target = self._at(j)
        if (
            target is None
            or target.kind not in NAME_KINDS
            or (target.kind == "word" and target.value in UPDATE_NOT_TARGETS)
        ):
            return
        self._record(j)

    def _clause_word(self, f: _Frame, word: str) -> None:
        if word == "with":
            if self._starts_with_clause():
                f.cte = "name"
        elif word == "from":
            if not self._after_distinct():
                f.from_active = True
                f.expect_table = True
        elif word in FROM_ENDS:
            if self._ends_from(word):
                f.from_active = False
                f.expect_table = False
        elif word == "into":
            f.expect_table = True
        elif word == "in":
            self._in_target()
        elif word == "update":
            self._update_target()

    def _word(self, f: _Frame, word: str) -> None:
        if word in COMPOUND_WORDS:
            self.compound = True
        if word == "join":
            self.outer = self.outer or bool(f.join_words & OUTER_WORDS)
            f.expect_table = True
            f.join_words.clear()
            return
        self._clause_word(f, word)
        if word in JOIN_WORDS:
            f.join_words.add(word)
        else:
            f.join_words.clear()


def statement_facts(sql: str) -> StatementFacts:
    return _Walker(tokenize(sql).tokens).run()


class SqliteCompiler:
    __slots__ = ()

    @staticmethod
    def compile(plan: TemplatePlan, /) -> tuple[Compiled, list[object]]:
        return compile_plan(plan)

    @staticmethod
    def compile_parts(strings: Sequence[str], /) -> Compiled:
        return compile_parts(strings)


COMPILER: Final[SqliteCompiler] = SqliteCompiler()
