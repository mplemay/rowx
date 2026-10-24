from pathlib import Path
from textwrap import dedent
from typing import TYPE_CHECKING

import pytest

from rowx.check.discover import discover_file, discover_source

if TYPE_CHECKING:
    from rowx.check.model import FileResult

PATH = Path("m.py")


def run(source: str) -> FileResult:
    return discover_source(PATH, dedent(source).encode())


def rules(result: FileResult) -> list[str]:
    return [d.rule for d in result.diagnostics]


def test_plain_call() -> None:
    result = run(
        """
        from rowx import query

        q = query[tuple[int, str]](t"select {1}, 'a'")
        """,
    )
    assert not result.diagnostics
    [call] = result.calls
    assert call.kind == "query"
    assert call.template is not None
    assert call.row_type_span is not None
    assert (call.row_type_span.line, call.row_type_span.col) == (4, 11)


def test_no_row_type() -> None:
    result = run('from rowx import query\nquery(t"select 1")\n')
    [call] = result.calls
    assert call.row_type is None
    assert call.row_type_span is None


def test_aliased_imports() -> None:
    result = run(
        """
        import rowx as rx
        from rowx import query as q, query_unchecked as qu

        a = q[int](t"select 1")
        b = rx.query[int](t"select 2")
        c = qu[int](t"select 3")
        d = rx.query_unchecked(t"select 4")
        """,
    )
    assert not result.diagnostics
    assert [c.kind for c in result.calls] == ["query", "query", "query_unchecked", "query_unchecked"]
    assert result.unchecked_count == 2


def test_dotted_import_binds_root() -> None:
    result = run('import rowx.errors\nrowx.query[int](t"select 1")\n')
    assert not result.diagnostics
    assert len(result.calls) == 1


def test_function_local_import() -> None:
    result = run(
        """
        def f():
            from rowx import query
            return query[int](t"select 1")

        def g():
            return query[int](t"select 2")
        """,
    )
    assert not result.diagnostics
    assert [c.span.line for c in result.calls] == [4]


def test_type_checking_import() -> None:
    result = run(
        """
        from typing import TYPE_CHECKING

        if TYPE_CHECKING:
            from rowx import query

        def f(q: query[int]) -> query[int]:
            return query[int](t"select 1")
        """,
    )
    assert not result.diagnostics
    assert len(result.calls) == 1


def test_try_import() -> None:
    result = run(
        """
        try:
            from rowx import query
        except ImportError:
            raise

        query[int](t"select 1")
        """,
    )
    assert len(result.calls) == 1


def test_inner_scope_wins() -> None:
    result = run(
        """
        from rowx import query

        def f(query):
            return query[int](t"select 1")
        """,
    )
    assert not result.calls
    assert not result.diagnostics


def test_unrelated_query_name_is_ignored() -> None:
    result = run('from other import query\nq = query\nquery[int](t"select 1")\n')
    assert not result.calls
    assert not result.diagnostics


def test_annotations_and_isinstance_are_allowed() -> None:
    result = run(
        """
        from rowx import query, query_unchecked

        x: query[int]
        y: list[query_unchecked] = []

        def f(a: query[int], *rest: query, k: query | None = None) -> query[int]:
            assert isinstance(a, query)
            assert isinstance(a, (query, query_unchecked))
            assert issubclass(type(a), query)
        """,
    )
    assert not result.diagnostics


@pytest.mark.parametrize(
    "source",
    [
        "Q = query[int]\n",
        "Q = query\n",
        "partial(query, t'select 1')\n",
        "class Sub(query[int]): ...\n",
        "class Sub(query): ...\n",
        "def f():\n    return query[int]\n",
        "items = [query]\n",
        "query.__name__\n",
        "print(rowx.query)\n",
        "getattr(rowx, 'query')\n",
        "lambda: query\n",
    ],
)
def test_indirect_use(source: str) -> None:
    result = run("import rowx\nfrom rowx import query\n" + source)
    assert "indirect-query" in rules(result)


def test_star_import_is_indirect() -> None:
    result = run("from rowx import *\n")
    assert rules(result) == ["indirect-query"]


def test_indirect_does_not_hide_other_hits() -> None:
    result = run(
        """
        from rowx import query

        Q = query[int]
        query[int](t"select 1")
        """,
    )
    assert rules(result) == ["indirect-query"]
    assert len(result.calls) == 1


@pytest.mark.parametrize(
    "extra",
    [
        "query = something()",
        "for query in items: pass",
        "def query(): ...",
        "class query: ...",
        "with ctx() as query: pass",
        "(query := 1)",
        "import query",
        "from other import query",
        "try:\n    pass\nexcept Exception as query:\n    pass",
        "match x:\n    case [query]:\n        pass",
    ],
)
def test_rebound_name_is_ambiguous(extra: str) -> None:
    result = run(f'from rowx import query\n{extra}\nquery[int](t"select 1")\n')
    assert rules(result) == ["indirect-query"]
    assert not result.calls


def test_rebound_parameter_is_ambiguous() -> None:
    result = run(
        """
        from rowx import query

        def f(query):
            query[int](t"select 1")
        """,
    )
    assert not result.calls


def test_rebound_module_alias_is_ambiguous() -> None:
    result = run('import rowx as rx\nrx = 1\nrx.query[int](t"select 1")\n')
    assert rules(result) == ["indirect-query"]


def test_comprehension_and_class_scopes() -> None:
    result = run(
        """
        from rowx import query

        class A:
            query = 1

            def m(self):
                return query[int](t"select 1")

        values = [query[int](t"select {i}") for i in range(3)]
        """,
    )
    assert not result.diagnostics
    assert len(result.calls) == 2


@pytest.mark.parametrize(
    "call",
    [
        'query[int](t"select 1", extra=1)',
        'query[int](sql=t"select 1")',
        'query[int](t"select 1", t"select 2")',
        "query[int](*args)",
        "query[int](**kwargs)",
        "query[int](sql)",
        'query[int](f"select 1")',
        'query[int]("select 1")',
        "query[int]()",
        "query[int](make())",
    ],
)
def test_call_shape_is_dynamic(call: str) -> None:
    result = run(f"from rowx import query\n{call}\n")
    assert rules(result) == ["dynamic-query"]
    assert not result.calls


def test_parenthesised_and_multiline_template() -> None:
    result = run('from rowx import query\nquery[int]((t"select "\n  t"1"))\n')
    assert not result.diagnostics
    assert len(result.calls) == 1


@pytest.mark.parametrize(
    "template",
    [
        't"select {x=}"',
        't"select {x!r}"',
        't"select {x!s}"',
        't"select {x:>5}"',
        't"select {t"inner"}"',
        't"select {(t"inner")}"',
    ],
)
def test_interpolation_is_dynamic(template: str) -> None:
    result = run(f"from rowx import query\nquery[int]({template})\n")
    assert rules(result) == ["dynamic-query"]
    assert not result.calls


def test_plain_interpolation_and_nested_call_are_fine() -> None:
    result = run(
        """
        from rowx import query

        query[int](t"select {x} where {y + 1} = {query[int](t'select 1')}")
        """,
    )
    assert not result.diagnostics
    assert len(result.calls) == 2


def test_query_unchecked_is_counted_and_skips_call_shape() -> None:
    result = run(
        """
        from rowx import query_unchecked

        query_unchecked[int](sql)
        query_unchecked[int](t"select {t'x'}")
        """,
    )
    assert not result.diagnostics
    assert result.unchecked_count == 2
    assert all(c.template is None and c.row_type is not None for c in result.calls)


def test_syntax_error_has_position() -> None:
    result = run("x = 1\ny = (\n")
    assert result.tree is None
    [diagnostic] = result.diagnostics
    assert diagnostic.rule == "syntax-error"
    assert diagnostic.span.line >= 2


def test_syntax_error_column() -> None:
    result = run("x = 1\ny = 1 +* 2\n")
    [diagnostic] = result.diagnostics
    assert (diagnostic.span.line, diagnostic.span.col) == (2, 8)


def test_decode_error() -> None:
    result = discover_source(PATH, b"x = 1\ny = '\xff'\n")
    [diagnostic] = result.diagnostics
    assert diagnostic.rule == "syntax-error"
    assert diagnostic.span.line == 2


def test_bom_is_stripped() -> None:
    result = discover_source(PATH, b'\xef\xbb\xbffrom rowx import query\nquery[int](t"select 1")\n')
    assert not result.diagnostics
    assert result.calls[0].span.col == 1


def test_coding_declaration() -> None:
    source = '# -*- coding: latin-1 -*-\nfrom rowx import query\nx = "é"; query[int](t"select 1")\n'
    result = discover_source(PATH, source.encode("latin-1"))
    assert not result.diagnostics
    [call] = result.calls
    assert call.span.col == 10


def test_unknown_coding_is_syntax_error() -> None:
    result = discover_source(PATH, b"# coding: nosuchcodec\nx = 1\n")
    assert rules(result) == ["syntax-error"]


def test_columns_are_code_points() -> None:
    result = run('from rowx import query\nx = "日本語"; query[int](t"select 1")\n')
    [call] = result.calls
    assert (call.span.line, call.span.col) == (2, 12)
    assert call.span.end_col == call.span.col + len('query[int](t"select 1")')


def test_crlf_lines() -> None:
    result = discover_source(PATH, b'from rowx import query\r\n\r\nquery[int](t"select 1")\r\n')
    assert result.calls[0].span.line == 3


def test_discover_file(tmp_path: Path) -> None:
    file = tmp_path / "a.py"
    file.write_text('from rowx import query\nquery[int](t"select 1")\n', encoding="utf-8")
    assert len(discover_file(file).calls) == 1


def test_type_parameter_shadows_import() -> None:
    result = run(
        """
        from rowx import query as Q

        def f[Q]() -> Q:
            return Q

        class C[Q]:
            x: Q

        type Alias[Q] = Q
        """,
    )
    assert not result.diagnostics


def test_class_names_are_visible_to_method_annotations() -> None:
    result = run(
        """
        from rowx import query

        class C:
            query = 1

            def m[T](self, x: query[T]) -> None: ...
        """,
    )
    assert not result.diagnostics


def test_walrus_in_definition_expressions_rebinds() -> None:
    for definition in (
        "def f(x=(query := other)): pass",
        "@(query := other)\ndef f(): pass",
        "class C((query := other)): pass",
    ):
        result = run(f'from rowx import query\n{definition}\nquery[int](t"select 1")\n')
        assert rules(result) == ["indirect-query"], definition
        assert not result.calls


def test_non_text_codec_is_syntax_error() -> None:
    result = discover_source(PATH, b"# coding: base64_codec\nx = 1\n")
    assert rules(result) == ["syntax-error"]
