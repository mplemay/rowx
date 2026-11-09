import os
from pathlib import Path
from textwrap import dedent
from typing import Final

import pytest

from rowx.check.construction import HELP, template_reports
from rowx.check.discover import discover_source
from rowx.check.params import ParamInference
from rowx.check.render import Report, render_json, render_text
from rowx.check.sourcemap import SourceMapper

EXPECTED: Final[Path] = Path(__file__).parent / "expected"
PATH: Final[Path] = Path("app/build.py")
SOURCE: Final[str] = dedent(
    """\
    from string.templatelib import Interpolation, Template
    from string.templatelib import Template as T

    from rowx import query_unchecked

    T2 = Template


    def build(x, parts, spec):
        Template(x)
        Template(*parts)
        t"a" + Template(x)
        T(x)
        Interpolation(x, "x", None, spec)
        t"a" + x
        query_unchecked(t"SELECT {x:{spec}}")
        return Template("lit", Interpolation(x, "x")), t"a" + t"b"
    """,
)


def reports(source: str) -> list[Report]:
    result = discover_source(PATH, dedent(source).encode())
    assert result.source is not None
    inference = ParamInference(result.tree) if result.tree is not None else None
    return template_reports(PATH, result.templates, SourceMapper(result.source), inference)


def lines(source: str) -> list[int]:
    return [report.diagnostic.span.line for report in reports(source)]


def golden(name: str, actual: str) -> None:
    path = EXPECTED / name
    if "ROWX_UPDATE_GOLDENS" in os.environ:
        path.write_text(actual)
    assert actual == path.read_text()


def test_text_golden() -> None:
    found = reports(SOURCE)
    assert {report.diagnostic.rule for report in found} == {"template-construction"}
    assert {report.help for report in found} == {HELP}
    golden("template_construction.txt", render_text(found, {PATH: SOURCE}))


def test_json_golden() -> None:
    golden("template_construction.json.txt", render_json(reports(SOURCE)))


def test_help_text_is_the_designed_one() -> None:
    assert HELP == "build dynamic SQL from t-string literals; `Template(...)` with a non-literal string is raw SQL"


def test_spans_point_at_the_offender() -> None:
    (report,) = reports("from string.templatelib import Template\nTemplate('a', x)\n")
    span = report.diagnostic.span
    assert (span.line, span.col, span.end_col) == (2, 15, 16)
    (report,) = reports("from string.templatelib import Template\nTemplate('a', b=x)\n")
    span = report.diagnostic.span
    assert (span.line, span.col, span.end_col) == (2, 15, 18)


def test_literal_forms_pass() -> None:
    assert not reports(
        """
        import string.templatelib
        from string.templatelib import Interpolation, Template
        Template()
        Template("a" "b", t"{x}", Interpolation(x, "x", "r", "ident"))
        Template(Interpolation(x, "x", conversion=None, format_spec=""))
        string.templatelib.Template("a")
        t"a" + t"b" + Template("c")
        t"a" + (t"b" + t"c")
        """,
    )


def test_spec_of_a_direct_argument_is_not_reported_twice() -> None:
    source = """
        from rowx import query, query_unchecked
        query_unchecked[int](t"SELECT {x:{s}}")
        query[int](t"SELECT {x:{s}}")
        query_unchecked[int](t"SELECT {t'{x:{s}}'}")
        h = t"{x:{s}}"
        query_unchecked[int](t"SELECT {x:ident}" + t"{y:{s}}")
        """
    assert lines(source) == [5, 6, 7]
    result = discover_source(PATH, dedent(source).encode())
    assert [d.rule for d in result.diagnostics] == ["dynamic-query", "dynamic-query"]


def test_evident_templates_are_accepted_as_operands() -> None:
    assert not reports(
        """
        from string.templatelib import Template
        base = t"a"
        def f(p: Template, q: "Template | None"):
            return t"a" + base, t"a" + p, p + t"a", t"a" + q
        """,
    )
    assert lines(
        """
        from string.templatelib import Template
        base = t"a"
        base = "b"
        def f(p):
            return t"a" + base, t"a" + p
        """,
    ) == [6, 6]


def test_file_without_query_sites_is_checked() -> None:
    assert lines("from string.templatelib import Template\nTemplate(x)\n") == [2]


@pytest.mark.parametrize(
    "source",
    [
        "from typing import Optional\nfrom string.templatelib import Template\ndef f(a: Optional[Template]): return t'a' + a\n",
        "import typing\nfrom string.templatelib import Template\ndef f(a: typing.Optional[Template]): return t'a' + a\n",
        "from typing import Optional as Maybe\nfrom string.templatelib import Template\ndef f(a: Maybe[Template]): return t'a' + a\n",
    ],
)
def test_optional_resolves_through_its_binding(source: str) -> None:
    assert not reports(source)


def test_a_user_optional_is_not_typing_optional() -> None:
    source = (
        "from string.templatelib import Template\nclass Optional: ...\ndef f(a: Optional[Template]): return t'a' + a\n"
    )
    assert lines(source) == [3]


def test_templatelib_class_imported_as_isinstance_is_a_template_use() -> None:
    source = "from string.templatelib import Template as isinstance\nisinstance('a', x)\n"
    assert lines(source) == [2]
    assert not reports("from string.templatelib import Template\nisinstance(x, Template)\n")


def test_alias_of_an_evident_template_is_evident() -> None:
    assert not reports(
        """
        from string.templatelib import Template
        def f(x: Template):
            y = x
            z = y
            return t"a" + z
        """,
    )


@pytest.mark.parametrize(
    "body",
    [
        "y = x\n            y = 'b'\n            return t'a' + y",
        "y = x\n            y += 'b'\n            return t'a' + y",
        "y = x\n            def g():\n                nonlocal y\n                y = 'b'\n            return t'a' + y",
        "y = (x := 'b')\n            return t'a' + y",
        "y = x\n            del y\n            y = 'b'\n            return t'a' + y",
    ],
)
def test_alias_with_another_binding_is_not_evident(body: str) -> None:
    source = f"""
        from string.templatelib import Template
        def f(x: Template):
            {body}
        """
    assert lines(source)


def test_class_pattern_is_an_inspection() -> None:
    assert not reports(
        """
        from string.templatelib import Template
        import string.templatelib
        def f(q):
            match q:
                case Template(strings=s):
                    return s
                case string.templatelib.Template():
                    return None
        """,
    )
    assert lines("from string.templatelib import Template\nmatch q:\n    case x:\n        y = Template\n") == [4]


@pytest.mark.parametrize(
    "use",
    [
        "from rowx_core._template import Template as T\nT(x)",
        "import rowx_core._template as m\nm.Template(x)",
        "from rowx_core._template import Interpolation\nInterpolation(1, 'a', None, s)",
        "import rowx_core._template\nrowx_core._template.Template(x)",
        "from rowx_core import _template\n_template.Template(x)",
        "from rowx_core import _template\ny = _template",
        "from rowx_core import _query\nT = _query.Template",
        "from rowx_core._template import *",
    ],
)
def test_private_rowx_modules_are_templatelib_reexports(use: str) -> None:
    assert lines(use)


def test_private_rowx_names_that_are_not_templatelib_are_fine() -> None:
    assert not reports("from rowx_core._query import helper\nfrom rowx import _core\nhelper(_core.foo)\n")


def test_a_function_imported_from_a_private_module_is_ordinary() -> None:
    assert not reports("from rowx_postgres._compiler import _is_ident_char as ok\nok('a')\ny = ok\n")
    assert not reports("from rowx_core._template import helper, _helper\nhelper(1)\n_helper(2)\n")


@pytest.mark.parametrize(
    "use",
    [
        "from typing import Annotated\nAnnotated[Template, 1](x)",
        "import typing\ntyping.Annotated[Template, 1](x)",
        "import typing_extensions as te\nte.Annotated[Template, 1](x)",
        "from typing import Annotated\nA = Annotated[Template, 1]\nA(x)",
    ],
)
def test_annotated_is_callable_at_runtime(use: str) -> None:
    assert lines("from string.templatelib import Template\n" + use)


def test_annotated_in_an_annotation_is_a_type() -> None:
    source = "from typing import Annotated\nfrom string.templatelib import Template\nx: Annotated[Template, 1] = 1\n"
    assert not reports(source + "def f(a: Annotated[Template, 1]) -> Annotated[Template, 2]: ...\n")


def test_issubclass_names_classes_in_both_arguments() -> None:
    header = "from string.templatelib import Template\n"
    assert not reports(header + "issubclass(Template, object)\nissubclass(Template, (Template, int))\n")
    assert not reports(header + "isinstance(x, Template)\n")
    assert lines(header + "issubclass(Template(y), object)\n") == [2]
    assert lines(header + "isinstance(Template, object)\n") == [2]


def test_value_pattern_is_an_inspection() -> None:
    header = "import string.templatelib\nmatch q:\n    case string.templatelib.Template:\n        pass\n"
    assert not reports(header)


def test_comparing_with_the_class_is_an_inspection() -> None:
    header = "import string.templatelib\nfrom string.templatelib import Template\n"
    source = "def f(t):\n    return type(t) is Template or type(t) != string.templatelib.Template\n"
    assert not reports(header + source)
    assert not reports(header + "def g(t: Template):\n    return type(t).__name__\n")
    assert lines(header + "def h(t: Template):\n    return type(t).__new__\n") == [4]
    assert lines(header + "y = Template if x else 1\n") == [3]
    assert lines(header + "y = Template < x\n") == [3]


def test_deeply_nested_file_is_a_syntax_error_not_a_crash() -> None:
    for source in ("y = " + " + ".join(["1"] * 3000) + "\n", "a" + ".b" * 5000 + "\n"):
        result = discover_source(PATH, source.encode())
        assert [(d.rule, d.message) for d in result.diagnostics] == [
            ("syntax-error", "the file is nested too deeply to analyse"),
        ]
