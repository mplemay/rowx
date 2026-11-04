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


@pytest.mark.parametrize(
    ("template", "reported"),
    [
        ('t"select {x:{spec}}"', 1),
        ('t"select {x:a{spec}b}"', 1),
        ('t"select {x:{spec}} {y:{other}}"', 2),
        ('t"select {x:ident}"', 0),
        ('t"select {x:>5}"', 0),
        ('t"select {x}"', 0),
        ('t"select {x!r}"', 0),
    ],
)
def test_query_unchecked_non_literal_format_spec_is_dynamic(template: str, reported: int) -> None:
    result = run(f"from rowx import query_unchecked\nquery_unchecked({template})\n")
    assert rules(result) == ["dynamic-query"] * reported
    assert result.unchecked_count == 1


def test_query_unchecked_with_a_non_template_argument_is_not_inspected() -> None:
    result = run("from rowx import query_unchecked\nquery_unchecked(make())\nquery_unchecked(*args)\n")
    assert not result.diagnostics
    assert result.unchecked_count == 2


def test_query_unchecked_dynamic_format_spec_is_anchored_at_the_interpolation() -> None:
    result = run('from rowx import query_unchecked\nquery_unchecked(t"select {a} {x:{spec}}")\n')
    (diagnostic,) = result.diagnostics
    assert (diagnostic.span.line, diagnostic.span.col, diagnostic.span.end_col) == (2, 30, 40)


def template_calls(result: FileResult) -> list[tuple[str, int]]:
    return [(c.name, c.span.line) for c in result.templates.calls]


def indirect_kinds(result: FileResult) -> list[tuple[str, str | None]]:
    return [(u.kind, u.name) for u in result.templates.indirect_uses]


@pytest.mark.parametrize(
    ("imports", "template", "interpolation"),
    [
        ("from string.templatelib import Template, Interpolation", "Template", "Interpolation"),
        ("from string.templatelib import Template as X, Interpolation as Y", "X", "Y"),
        ("import string.templatelib", "string.templatelib.Template", "string.templatelib.Interpolation"),
        ("import string.templatelib as m", "m.Template", "m.Interpolation"),
        ("from string import templatelib", "templatelib.Template", "templatelib.Interpolation"),
        ("from string import templatelib as m", "m.Template", "m.Interpolation"),
        ("import string", "string.templatelib.Template", "string.templatelib.Interpolation"),
        ("import string as s", "s.templatelib.Template", "s.templatelib.Interpolation"),
    ],
)
def test_templatelib_import_forms(imports: str, template: str, interpolation: str) -> None:
    result = run(f'{imports}\nt = {template}("a", {interpolation}(1, "x"))\n')
    assert not result.diagnostics
    assert template_calls(result) == [("Template", 2), ("Interpolation", 2)]
    assert not result.templates.indirect_uses
    template_call, interpolation_call = result.templates.calls
    assert len(template_call.node.args) == 2
    assert [type(a).__name__ for a in interpolation_call.node.args] == ["Constant", "Constant"]


def test_templatelib_unrelated_modules_not_resolved() -> None:
    result = run(
        """
        from other.templatelib import Template
        from string import Template as Legacy
        import string.other
        import string.templatelib.extra as e
        from . import templatelib
        Template("x")
        Legacy("x")
        string.other.Template("x")
        e.Template("x")
        templatelib.Template("x")
        """,
    )
    assert not result.templates.calls
    assert not result.templates.indirect_uses


def test_templatelib_shadowed_by_inner_scope() -> None:
    result = run(
        """
        from string.templatelib import Template

        def f(Template):
            return Template("x")

        def g():
            Template = str
            return Template("x")

        def h():
            return Template("x")

        class C:
            Template = str
            x = Template("x")
            def m(self):
                return Template("y")

        lambda Template: Template("z")
        """,
    )
    assert template_calls(result) == [("Template", 12), ("Template", 18)]
    assert not result.templates.indirect_uses


def test_templatelib_ambiguous_name() -> None:
    result = run(
        """
        from string.templatelib import Template
        Template = str
        Template("x")
        print(Template)
        """,
    )
    assert not result.templates.calls
    assert indirect_kinds(result) == [("ambiguous", "Template"), ("ambiguous", "Template")]


def test_templatelib_ambiguous_module() -> None:
    result = run(
        """
        import string
        string = 1
        string.templatelib.Template("x")
        """,
    )
    assert not result.templates.calls
    assert indirect_kinds(result) == [("ambiguous", "Template")]


def test_templatelib_try_except_import_is_ambiguous() -> None:
    result = run(
        """
        try:
            from string.templatelib import Template
        except ImportError:
            Template = None
        Template("x")
        """,
    )
    assert indirect_kinds(result) == [("ambiguous", "Template")]


def test_templatelib_type_checking_import() -> None:
    result = run(
        """
        from typing import TYPE_CHECKING

        if TYPE_CHECKING:
            from string.templatelib import Template

        def f(t: Template) -> Template:
            return Template("x")
        """,
    )
    assert template_calls(result) == [("Template", 8)]
    assert not result.templates.indirect_uses


def test_templatelib_annotation_and_isinstance_are_not_indirect() -> None:
    result = run(
        """
        import string.templatelib as m
        from string.templatelib import Template

        x: Template = f()
        y: m.Template = f()
        def g(a: "Template", b: m.Interpolation) -> m.Template: ...
        isinstance(x, Template)
        issubclass(type(y), (m.Template,))
        """,
    )
    assert not result.templates.indirect_uses


def test_templatelib_star_import() -> None:
    result = run("from string.templatelib import *\nTemplate('x')\n")
    assert not result.templates.calls
    [use] = result.templates.indirect_uses
    assert (use.kind, use.name, use.span.line) == ("star-import", None, 1)
    assert not run("from string import *\n").templates.indirect_uses


@pytest.mark.parametrize(
    ("use", "kind"),
    [
        ("T = Template", "value"),
        ("print(Template)", "value"),
        ("xs = [Template]", "value"),
        ("T = m.Template", "value"),
        ("T = string.templatelib.Template", "value"),
        ("class C(Template): ...", "value"),
        ("Template.__new__(Template)", "attribute"),
        ("m.Template.__new__", "attribute"),
        ("string.templatelib.Template.__call__", "attribute"),
        ("Template.__name__", "attribute"),
    ],
)
def test_templatelib_indirect_uses(use: str, kind: str) -> None:
    result = run(
        f"""
        import string
        import string.templatelib as m
        from string.templatelib import Template
        {use}
        """,
    )
    assert not result.diagnostics
    assert kind in {u.kind for u in result.templates.indirect_uses}
    assert {u.name for u in result.templates.indirect_uses} == {"Template"}
    assert all(u.span.line == 5 for u in result.templates.indirect_uses)


def test_templatelib_interpolation_indirect_use() -> None:
    result = run("from string.templatelib import Interpolation as I\nf = I\n")
    assert indirect_kinds(result) == [("value", "Interpolation")]


def test_templatelib_class_of_template_string() -> None:
    result = run(
        """
        a = type(t"x")("y")
        b = t"x".__class__("y")
        c = type(t"x")
        d = type("x")("y")
        """,
    )
    assert [(u.kind, u.name, u.span.line) for u in result.templates.indirect_uses] == [
        ("type-call", None, 2),
        ("type-call", None, 3),
        ("value", "Template", 4),
    ]


def test_templatelib_type_shadowed_is_not_class_of_template() -> None:
    result = run("def f(type):\n    return type(t'x')('y')\n")
    assert not result.templates.indirect_uses


def test_templatelib_call_arguments_recorded() -> None:
    result = run(
        """
        from string.templatelib import Template, Interpolation

        def build(parts, v):
            return Template(*parts), Template("a", t"b{v}", Interpolation(v, "v", None, "")), Template(v, k=1)
        """,
    )
    template_a, template_b, interp, template_c = result.templates.calls
    assert [c.name for c in (template_a, template_b, interp, template_c)] == [
        "Template",
        "Template",
        "Interpolation",
        "Template",
    ]
    assert [type(a).__name__ for a in template_a.node.args] == ["Starred"]
    assert [type(a).__name__ for a in template_b.node.args] == ["Constant", "TemplateStr", "Call"]
    assert len(interp.node.args) == 4
    assert [k.arg for k in template_c.node.keywords] == ["k"]
    assert (template_a.span.line, template_a.span.col) == (5, 12)


def test_templatelib_nested_call_in_query_argument_recorded() -> None:
    result = run(
        """
        from rowx import query_unchecked
        from string.templatelib import Template

        q = query_unchecked(Template("select 1"))
        """,
    )
    assert template_calls(result) == [("Template", 5)]
    assert not result.diagnostics


def test_templatelib_analysed_without_query_sites() -> None:
    result = run(
        """
        import string.templatelib as tl

        def helper(x):
            return tl.Template(x)
        """,
    )
    assert not result.calls
    assert not result.diagnostics
    assert template_calls(result) == [("Template", 5)]


def test_templatelib_analysed_in_function_default_and_decorator() -> None:
    result = run(
        """
        from string.templatelib import Template

        @deco(Template("a"))
        def f(x=Template("b"), *, y=Template):
            pass
        """,
    )
    assert template_calls(result) == [("Template", 4), ("Template", 5)]
    assert indirect_kinds(result) == [("value", "Template")]


def test_templatelib_nothing_reported_as_diagnostic() -> None:
    result = run(
        """
        from string.templatelib import *
        import string.templatelib as m
        T = m.Template
        m.Template(1)
        t"a" + 1
        t"{x:{s}}"
        """,
    )
    assert not result.diagnostics
    assert result.templates.calls
    assert result.templates.indirect_uses
    assert result.templates.concats
    assert result.templates.dynamic_specs


def test_templatelib_concat_facts() -> None:
    result = run(
        """
        a = t"a" + t"b"
        b = t"a" + x
        c = x + t"a"
        d = t"a" + t"b" + y
        e = 1 + 2
        f = x + y
        g = t"a" - x
        h = (x + y) + z
        """,
    )
    assert [(type(c.operand).__name__, c.span.line) for c in result.templates.concats] == [
        ("TemplateStr", 2),
        ("Name", 3),
        ("Name", 4),
        ("TemplateStr", 5),
        ("Name", 5),
    ]
    assert [c.operand_span.col for c in result.templates.concats][1] == 12


def test_templatelib_dynamic_format_specs() -> None:
    result = run(
        """
        a = t"{x:{s}}"
        b = t"{x:lit}"
        c = t"{x}"
        d = t"{x:a{s}b}"
        e = t"{x!r:{s}}"
        def w(spec):
            return t"select {v:{spec}}"
        """,
    )
    assert [(s.span.line, s.direct_argument_of) for s in result.templates.dynamic_specs] == [
        (2, None),
        (5, None),
        (6, None),
        (8, None),
    ]
    assert not result.diagnostics


def test_templatelib_dynamic_format_spec_direct_arguments() -> None:
    result = run(
        """
        from rowx import query, query_unchecked

        a = query_unchecked(t"select {x:{s}}")
        b = query[int](t"select {x:{s}}")
        c = query_unchecked(t"select {x:{s}}" + t"{y}")
        d = query_unchecked(t"select {x:{s}}", t"")
        e = query_unchecked(t"select {t'{y:{s}}'}")
        f = query_unchecked(t"select {x:{y:{s}}}")
        """,
    )
    assert [(s.span.line, s.direct_argument_of) for s in result.templates.dynamic_specs] == [
        (4, "query_unchecked"),
        (5, "query"),
        (6, None),
        (7, None),
        (8, None),
        (9, "query_unchecked"),
    ]
    assert rules(result).count("dynamic-query") == 3


def test_templatelib_facts_default_empty() -> None:
    result = run("x = 1\n")
    assert result.templates.calls == ()
    assert result.templates.indirect_uses == ()
    assert result.templates.concats == ()
    assert result.templates.dynamic_specs == ()


@pytest.mark.parametrize("order", [0, 1])
def test_query_ambiguity_with_templatelib_name_is_order_independent(order: int) -> None:
    imports = ["from string.templatelib import Template as q", "from rowx import query as q"]
    result = run("\n".join(imports[::-1] if order else imports) + '\nq[int](t"select 1")\n')
    assert rules(result) == ["indirect-query"]
    assert not result.calls
    assert ("ambiguous", "Template") in indirect_kinds(result)


def test_templatelib_ambiguous_indirect_use_is_recorded_for_mixed_name() -> None:
    result = run("from rowx import query as q\nfrom string.templatelib import Template as q\nx = q\n")
    assert rules(result) == ["indirect-query"]
    assert indirect_kinds(result) == [("ambiguous", "Template")]


@pytest.mark.parametrize(
    ("source", "kind", "name", "called"),
    [
        (
            "from string import templatelib\ntemplatelib._template_unpickle(('x',), ())",
            None,
            "_template_unpickle",
            True,
        ),
        ("from string.templatelib import _template_unpickle\n_template_unpickle(('x',), ())", "private", None, True),
        (
            "import string.templatelib\nstring.templatelib._template_unpickle(('x',), ())",
            None,
            "_template_unpickle",
            True,
        ),
        ("from string import templatelib\nx = templatelib._template_unpickle", "private", "_template_unpickle", False),
        ("from string import templatelib\nx = templatelib.__dict__", "private", "__dict__", False),
    ],
)
def test_templatelib_non_public_names(source: str, kind: str | None, name: str | None, called: bool) -> None:  # ruff: ignore[boolean-type-hint-positional-argument]
    result = run(source)
    if called:
        [call] = result.templates.calls
        assert call.private
        assert call.name == "_template_unpickle"
    else:
        assert not result.templates.calls
    uses = indirect_kinds(result)
    if kind is not None:
        assert ("private", name or "_template_unpickle") in uses


def test_templatelib_public_names_are_not_private() -> None:
    result = run(
        "from string import templatelib\nfrom string.templatelib import convert\nconvert(1, 's')\ntemplatelib.convert(1, 's')\n"
    )
    assert not result.templates.calls
    assert not result.templates.indirect_uses


@pytest.mark.parametrize(
    "use",
    [
        "m = templatelib",
        "f(templatelib)",
        "d = {'m': templatelib}",
        "vars(templatelib)",
        "m = string.templatelib",
        "f(string)",
        "t = (string,)",
    ],
)
def test_templatelib_module_used_as_value(use: str) -> None:
    result = run(f"import string\nfrom string import templatelib\n{use}\n")
    assert {u.kind for u in result.templates.indirect_uses} == {"module-value"}


def test_templatelib_module_as_attribute_base_is_not_a_value() -> None:
    result = run(
        """
        import string
        from string import templatelib
        a = string.ascii_letters + string.digits
        b = templatelib.convert
        c = string.Template("x")
        """,
    )
    assert not result.templates.indirect_uses
    assert not result.templates.calls


@pytest.mark.parametrize(
    ("use", "kind", "name"),
    [
        ("type(q)(x)", "type-call", None),
        ("q.__class__(x)", "type-call", None),
        ("T = type(q)", "value", "Template"),
        ("T = q.__class__", "attribute", "Template"),
        ("type(Template('a'))(x)", "type-call", None),
    ],
)
def test_templatelib_type_of_evident_template(use: str, kind: str, name: str | None) -> None:
    result = run(
        f"""
        from string.templatelib import Template
        q = t"a"
        def f(r: Template, x):
            {use}
        {use}
        """,
    )
    assert [(u.kind, u.name) for u in result.templates.indirect_uses if u.span.line == 6] == [(kind, name)]


def test_templatelib_type_of_unknown_receiver_is_not_recorded() -> None:
    result = run(
        """
        class C:
            def m(self, q, x):
                a = type(self)(x)
                b = type(q)(x)
                c = q.__class__(x)
                d = self.__class__
        q = t"a"
        q = t"b"
        type(q)(1)
        """,
    )
    assert not result.templates.indirect_uses


def test_templatelib_dunder_on_t_string_literal() -> None:
    result = run("a = t'a'.__add__\nb = t'a'.__class__\nc = t'a'.strings\nd = t'a'.__add__(1)\n")
    assert [(u.kind, u.span.line) for u in result.templates.indirect_uses] == [
        ("attribute", 1),
        ("attribute", 2),
        ("attribute", 4),
    ]


@pytest.mark.parametrize(
    "source",
    [
        "from string.templatelib import Template\ntype T = Template\n",
        "from typing import TypeAlias\nfrom string.templatelib import Template\nT: TypeAlias = Template\n",
        "import typing\nimport string.templatelib as m\nT: typing.TypeAlias = m.Template\n",
    ],
)
def test_templatelib_type_aliases(source: str) -> None:
    assert [u.kind for u in run(source).templates.indirect_uses] == ["alias"]


def test_templatelib_reexport_in_all() -> None:
    result = run(
        """
        from string.templatelib import Template as T, Interpolation, convert
        from string import templatelib
        __all__ = ["T", "templatelib", "convert", "missing", 3]
        __all__ += ["Interpolation"]
        """,
    )
    assert [(u.kind, u.name, u.span.line) for u in result.templates.indirect_uses] == [
        ("re-export", "T", 4),
        ("re-export", "templatelib", 4),
        ("re-export", "Interpolation", 5),
    ]


@pytest.mark.parametrize(
    "use",
    [
        "x: list[Template] = []",
        "y = list[Template]",
        "class P(Protocol[Template]): ...",
        "z = dict[str, Template]",
        "w = cast(Template, v)",
        "w = typing.cast(Template, v)",
        "w = cast(typ=Template, val=v)",
        "def g(a: 'Template') -> 'Template': ...",
        "w = d[m.Template]",
        "w = list[string.templatelib.Template]",
    ],
)
def test_templatelib_type_level_uses_are_not_recorded(use: str) -> None:
    result = run(
        f"""
        import string
        import typing
        import string.templatelib as m
        from typing import Protocol, cast
        from string.templatelib import Template
        {use}
        """,
    )
    assert not result.templates.indirect_uses


@pytest.mark.parametrize(
    "use",
    ["T = Template", "f(Template)", "class C(Template): ...", "def g(a=Template): ...", "cast(int, Template)"],
)
def test_templatelib_runtime_value_uses_are_still_recorded(use: str) -> None:
    result = run(
        f"""
        from typing import cast
        from string.templatelib import Template
        {use}
        """,
    )
    assert [u.kind for u in result.templates.indirect_uses] == ["value"]


def test_templatelib_call_inside_subscript_and_cast_is_recorded() -> None:
    result = run(
        """
        from typing import cast
        from string.templatelib import Template
        a = d[Template("x")]
        b = cast(Template, Template("y"))
        """,
    )
    assert len(result.templates.calls) == 2
    assert not result.templates.indirect_uses


def test_templatelib_type_of_annotated_parameter_and_decorator() -> None:
    result = run(
        """
        from string.templatelib import Template
        @Template
        def g(r: Template, x):
            return type(r)(x)
        """,
    )
    assert [(u.kind, u.span.line) for u in result.templates.indirect_uses] == [("value", 3), ("type-call", 5)]


def test_templatelib_parameter_annotation_resolves_in_the_enclosing_scope() -> None:
    result = run(
        """
        from string.templatelib import Template
        def f(q: Template, sql):
            Template = str
            return type(q)(sql)
        """,
    )
    assert [(u.kind, u.span.line) for u in result.templates.indirect_uses] == [("type-call", 5)]


def test_templatelib_import_in_a_body_does_not_type_an_outer_annotation() -> None:
    result = run(
        """
        class Template: ...
        def f(q: Template, sql):
            from string.templatelib import Template
            return type(q)(sql)
        """,
    )
    assert not result.templates.indirect_uses


def test_templatelib_annotation_in_a_nested_function_uses_the_enclosing_function() -> None:
    result = run(
        """
        def outer(sql):
            from string.templatelib import Template
            def inner(q: Template):
                return type(q)(sql)
            return inner
        """,
    )
    assert [(u.kind, u.span.line) for u in result.templates.indirect_uses] == [("type-call", 5)]


def test_templatelib_method_annotation_sees_a_shadowing_class_body() -> None:
    result = run(
        """
        from string.templatelib import Template
        class C:
            Template = str
            def m(self, q: Template, sql):
                return type(q)(sql)
        """,
    )
    assert not result.templates.indirect_uses
