import ast
import warnings
from pathlib import Path
from textwrap import dedent
from typing import TYPE_CHECKING, Final

import pytest

from rowx.__tests__.sourcegen import Case, Placed, build_case, line_col, random_atoms, seeded
from rowx._template import compile_parts, compile_template
from rowx.check.discover import discover_source
from rowx.check.sourcemap import SourceMap, SourceMapper

if TYPE_CHECKING:
    from rowx.check.model import Span


def build(source: str, index: int = 0) -> tuple[SourceMap, str]:
    source = dedent(source)
    result = discover_source(Path("m.py"), source.encode())
    assert not result.diagnostics
    template = result.calls[index].template
    assert template is not None
    return SourceMapper(source).build(template), source


def text_at(source: str, span: Span) -> str:
    lines = source.splitlines(keepends=True)
    if span.line == span.end_line:
        return lines[span.line - 1][span.col - 1 : span.end_col - 1]
    first = lines[span.line - 1][span.col - 1 :]
    middle = "".join(lines[span.line : span.end_line - 1])
    return first + middle + lines[span.end_line - 1][: span.end_col - 1]


def runtime_sql(source: str, index: int = 0) -> str:
    templates = [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Call) and n.args]
    call = templates[index]
    expression = compile(ast.Expression(call.args[0]), "<sample>", "eval")
    compiled, _ = compile_template(eval(expression, {"x": 1, "y": 1}), dynamic=False)  # ruff: ignore[suspicious-eval-usage]
    return compiled.sql


def test_leading_non_ascii() -> None:
    sm, source = build('from rowx import query\nq = query(t"é select {1} é x")\n')
    assert sm.sql == "é select $1 é x"
    assert text_at(source, sm.locate(1)) == "é"
    assert sm.locate(1).col == 13
    assert text_at(source, sm.locate(3)) == "s"
    assert text_at(source, sm.locate(10)) == "{1}"
    assert text_at(source, sm.locate(13)) == "é"


def test_non_ascii_after_interpolation_is_code_points() -> None:
    sm, source = build('from rowx import query\nq = query(t"ééé{1}é")\n')
    span = sm.locate(sm.sql.rindex("é") + 1)
    assert text_at(source, span) == "é"
    assert span.col == 19


def test_escapes() -> None:
    source = 'from rowx import query\nq = query(t"a\\n\\x41\\u00e9\\101\\N{BULLET}\\\\ b")\n'
    sm, _ = build(source)
    assert sm.sql == runtime_sql(source)
    assert sm.sql == "a\nA\u00e9A\u2022\\ b"
    pieces = [text_at(source, sm.locate(i + 1)) for i in range(len(sm.sql))]
    assert pieces == ["a", "\\n", "\\x41", "\\u00e9", "\\101", "\\N{BULLET}", "\\\\", " ", "b"]
    assert all(sm.exact)


def test_backslash_newline_is_dropped() -> None:
    source = 'from rowx import query\nq = query(t"ab\\\ncd")\n'
    sm, _ = build(source)
    assert sm.sql == "abcd"
    assert (sm.locate(3).line, sm.locate(3).col) == (3, 1)


def test_doubled_braces() -> None:
    source = 'from rowx import query\nq = query(t"a {{ b }} {1} c")\n'
    sm, src = build(source)
    assert sm.sql == "a { b } $1 c"
    assert sm.sql == runtime_sql(source)
    assert text_at(src, sm.locate(3)) == "{"
    assert text_at(src, sm.locate(5)) == "b"
    assert text_at(src, sm.locate(7)) == "}"
    assert text_at(src, sm.locate(10)) == "{1}"
    assert text_at(src, sm.locate(12)) == "c"
    assert all(sm.exact)


def test_raw_string() -> None:
    source = 'from rowx import query\nq = query(rt"a\\n{{ \\x41")\n'
    sm, src = build(source)
    assert sm.sql == runtime_sql(source)
    assert sm.sql == "a\\n{ \\x41"
    assert [text_at(src, sm.locate(i + 1)) for i in range(len(sm.sql))] == list(sm.sql)


def test_triple_quoted_multiline() -> None:
    source = 'from rowx import query\nq = query(t"""\n  select {x},\n    b\n""")\n'
    sm, src = build(source)
    assert sm.sql == runtime_sql(source)
    assert sm.sql == "\n  select $1,\n    b\n"
    b = sm.sql.index("b")
    assert (sm.locate(b + 1).line, sm.locate(b + 1).col) == (4, 5)
    assert text_at(src, sm.locate(sm.sql.index("$") + 1)) == "{x}"
    assert text_at(src, sm.locate(b + 1)) == "b"


def test_multiline_interpolation() -> None:
    source = 'from rowx import query\nq = query(t"a {\n  x\n} b")\n'
    sm, src = build(source)
    span = sm.locate(sm.sql.index("$") + 1)
    assert (span.line, span.end_line) == (2, 4)
    assert text_at(src, span) == "{\n  x\n}"


def test_crlf() -> None:
    source = 'from rowx import query\r\nq = query(t"""\r\na\r\nb {x}\r\n""")\r\n'
    sm, _ = build(source)
    assert sm.sql == "\na\nb $1\n"
    assert sm.sql == runtime_sql(source)
    assert all(sm.exact)
    b = sm.sql.index("b")
    assert (sm.locate(b + 1).line, sm.locate(b + 1).col) == (4, 1)
    assert sm.locate(sm.sql.index("$") + 1).line == 4


def test_implicit_concatenation() -> None:
    source = 'from rowx import query\nq = query(\n    (t"select {x} "\n     t"from t "\n     t"where a = {y}")\n)\n'
    sm, src = build(source)
    assert sm.sql == "select $1 from t where a = $2"
    assert sm.sql == runtime_sql(source)
    assert all(sm.exact)
    f = sm.sql.index("from")
    assert (sm.locate(f + 1).line, sm.locate(f + 1).col) == (4, 8)
    w = sm.sql.index("where")
    assert (sm.locate(w + 1).line, sm.locate(w + 1).col) == (5, 8)
    assert text_at(src, sm.locate(sm.sql.index("$2") + 1)) == "{y}"


def test_error_after_interpolation() -> None:
    sm, src = build('from rowx import query\nq = query(t"select {x} frm t")\n')
    assert sm.sql == "select $1 frm t"
    assert text_at(src, sm.locate(sm.sql.index("frm") + 1)) == "f"
    assert text_at(src, sm.locate(8)) == "{x}"
    assert text_at(src, sm.locate(9)) == "{x}"


def test_end_of_input() -> None:
    sm, src = build('from rowx import query\nq = query(t"select {x} from")\n')
    span = sm.locate(len(sm.sql) + 1)
    assert (span.line, span.col) == (2, 28)
    assert span.end_col == span.col
    assert text_at(src, sm.locate(len(sm.sql))) == "m"


def test_end_of_input_after_interpolation() -> None:
    sm, _ = build('from rowx import query\nq = query(t"select {x}")\n')
    span = sm.locate(len(sm.sql) + 1)
    assert (span.line, span.col) == (2, 23)


def test_adjacency_spaces() -> None:
    source = 'from rowx import query\nq = query(t"a{x}b{y}")\n'
    sm, src = build(source)
    assert sm.sql == "a $1 b $2"
    assert sm.sql == runtime_sql(source)
    assert text_at(src, sm.locate(1)) == "a"
    # the inserted space maps to the following placeholder
    assert text_at(src, sm.locate(2)) == "{x}"
    assert text_at(src, sm.locate(5)) == "{x}"
    assert text_at(src, sm.locate(6)) == "b"
    assert text_at(src, sm.locate(8)) == "{y}"


def test_nested_string_in_interpolation_is_ignored() -> None:
    source = "from rowx import query\nq = query(t\"a {x if y else 'q{'} b {y}\")\n"
    sm, src = build(source)
    assert sm.sql == "a $1 b $2"
    assert text_at(src, sm.locate(6)) == "b"
    assert all(sm.exact)


def test_inexact_segment_falls_back_to_segment_start() -> None:
    source = 'from rowx import query\nq = query(t"ab{x}cd")\n'
    result = discover_source(Path("m.py"), source.encode())
    template = result.calls[0].template
    assert template is not None
    # a Constant that disagrees with the source text
    template.values[0] = ast.Constant("zz")
    sm = SourceMapper(source).build(template)
    assert sm.exact == (False, True)
    assert sm.sql == "zz $1 cd"
    first = sm.locate(2)
    assert (first.line, first.col, first.end_col) == (2, 13, 13)
    assert sm.locate(1) == first


@pytest.mark.parametrize(
    "literal",
    [
        't"select {x}, {y}"',
        't"é\\n\\x41 {{ }} {x}"',
        'rt"a\\n {{ {x}"',
        't"""\n  a {x}\n  b\n"""',
        '(t"a "\n t"{x}"\n t" b{y}")',
        't"{x}{y}"',
        't"{x}a"',
        't""',
    ],
)
def test_sql_matches_runtime(literal: str) -> None:
    source = f"from rowx import query\nq = query(\n{literal}\n)\n"
    sm, src = build(source)
    assert sm.sql == runtime_sql(source)
    assert len(sm.spans) == len(sm.sql)
    assert all(sm.exact)
    for i, ch in enumerate(sm.sql):
        piece = text_at(src, sm.locate(i + 1))
        assert piece, (i, ch)
        if ch not in " \n" and piece[0] not in "\\{":
            assert piece == ch


def test_unknown_escape_keeps_backslash() -> None:
    source = 'from rowx import query\nq = query(t"a\\q b")\n'
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        sm, src = build(source)
    assert sm.sql == "a\\q b"
    assert [text_at(src, sm.locate(i + 1)) for i in range(len(sm.sql))] == ["a", "\\", "q", " ", "b"]


SEEDS: Final = range(400)


def generated_sql(source: str) -> str:
    with warnings.catch_warnings():
        # a generated unknown escape is a SyntaxWarning, which pytest turns into an error
        warnings.simplefilter("ignore", SyntaxWarning)
        tree = ast.parse(source)
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call) and n.args)
    code = compile(ast.Expression(call.args[0]), "<case>", "eval")
    namespace = {"x": 1, "y": 2, "f": lambda *_: 3}
    compiled, _ = compile_template(eval(code, namespace), dynamic=False)  # ruff: ignore[suspicious-eval-usage]
    return compiled.sql


def assert_span(case: Case, span: Span, placed: Placed) -> None:
    assert (span.line, span.col) == line_col(case.source, placed.offset), placed
    # `{{` and `}}` are two source characters for one decoded one
    if not placed.loose:
        assert (span.end_line, span.end_col) == line_col(case.source, placed.offset + placed.length), placed


def map_case(case: Case) -> SourceMap:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        result = discover_source(Path("m.py"), case.source.encode())
    assert not result.diagnostics
    template = result.calls[0].template
    assert template is not None
    return SourceMapper(case.source).build(template)


@pytest.mark.parametrize("seed", SEEDS)
def test_generated_sql_and_carets_match_the_runtime(seed: int) -> None:
    rng = seeded(seed)
    case = build_case(rng, random_atoms(rng), safe=False)
    sm = map_case(case)
    # the compiled SQL is what the runtime builds from the evaluated t-string
    assert sm.sql == generated_sql(case.source), case.source
    assert all(sm.exact), case.source
    assert len(sm.spans) == len(sm.sql)
    assert len(sm.holes) == len(case.holes)
    compiled = compile_parts(["".join(p.text for p in segment) for segment in case.segments])
    assert compiled.sql == sm.sql
    for index, (segment, (begin, finish)) in enumerate(zip(case.segments, compiled.segments, strict=True)):
        text = "".join(p.text for p in segment)
        # a space is inserted before an identifier character that follows a placeholder
        lead = 0 if sm.sql[begin : begin + len(text)] == text else 1
        for offset in range(begin, finish):
            span = sm.spans[offset]
            slot = offset - begin - lead
            if 0 <= slot < len(segment):
                assert_span(case, span, segment[slot])
            elif slot < 0:
                assert_span(case, span, case.holes[index - 1])
            else:
                assert_span(case, span, case.holes[index])
    for hole, (begin, finish) in zip(case.holes, compiled.placeholders, strict=True):
        for offset in range(begin, finish):
            assert_span(case, sm.spans[offset], hole)
