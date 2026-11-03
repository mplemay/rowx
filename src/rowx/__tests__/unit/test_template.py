from string.templatelib import Interpolation, Template
from typing import Final

import pytest

from rowx._template import MAX_PARAMETERS, Compiled, compile_parts, compile_template

X = 1
Y = 2


def check(c: Compiled, sql: str, params: list[str], segments: list[str]) -> None:
    assert c.sql == sql
    assert [c.sql[a:b] for a, b in c.placeholders] == params
    assert [c.sql[a:b] for a, b in c.segments] == segments
    assert len(c.segments) == len(c.placeholders) + 1


def test_repeated_expression_is_two_params() -> None:
    c, values = compile_template(t"{X}{X}", dynamic=False)
    check(c, "$1 $2", ["$1", "$2"], ["", " ", ""])
    assert values == [1, 1]


def test_adjacency_after_identifier() -> None:
    c, values = compile_template(t"SELECT 1 AS x{Y}", dynamic=False)
    check(c, "SELECT 1 AS x $1", ["$1"], ["SELECT 1 AS x ", ""])
    assert values == [2]


def test_adjacency_before_identifier() -> None:
    c, _ = compile_template(t"{X}x", dynamic=False)
    check(c, "$1 x", ["$1"], ["", " x"])


def test_no_space_next_to_punctuation() -> None:
    c, _ = compile_template(t"a = ({X}, {Y})", dynamic=False)
    check(c, "a = ($1, $2)", ["$1", "$2"], ["a = (", ", ", ")"])


def test_non_ascii_is_identifier_char() -> None:
    c, _ = compile_template(t"é{X}", dynamic=False)
    assert c.sql == "é $1"


def test_compile_parts() -> None:
    c = compile_parts(["a", "b", "c"])
    check(c, "a $1 b $2 c", ["$1", "$2"], ["a ", " b ", " c"])


def test_compile_parts_no_params() -> None:
    check(compile_parts(["SELECT 1"]), "SELECT 1", [], ["SELECT 1"])


def test_nested_flatten_and_renumber() -> None:
    inner = t"b = {Y}"
    c, values = compile_template(t"a = {X} AND {inner} AND c = {X}", dynamic=True)
    assert c.sql == "a = $1 AND b = $2 AND c = $3"
    assert [c.sql[a:b] for a, b in c.placeholders] == ["$1", "$2", "$3"]
    assert values == [1, 2, 1]


def test_ident_single_and_tuple() -> None:
    s, t_ = "s", "t"
    c, values = compile_template(t"SELECT * FROM {(s, t_):ident}", dynamic=True)
    assert c.sql == 'SELECT * FROM "s"."t"'
    assert values == []
    c, _ = compile_template(t"SELECT {'a"b':ident}", dynamic=True)
    assert c.sql == 'SELECT "a""b"'


def test_ident_does_not_add_param_segments() -> None:
    name = "t"
    c, _ = compile_template(t"FROM {name:ident} WHERE x = {X}", dynamic=True)
    check(c, 'FROM "t" WHERE x = $1', ["$1"], ['FROM "t" WHERE x = ', ""])


def test_ident_limit() -> None:
    ok, bad = "a" * 63, "a" * 64
    compile_template(t"{ok:ident}", dynamic=True)
    with pytest.raises(TypeError):
        compile_template(t"{bad:ident}", dynamic=True)
    multi = "é" * 32
    with pytest.raises(TypeError):
        compile_template(t"{multi:ident}", dynamic=True)


@pytest.mark.parametrize("name", ["", "a\x00b", 1, (), ("a", ""), ("a", 1), None])
def test_ident_invalid(name: object) -> None:
    with pytest.raises(TypeError):
        compile_template(t"{name:ident}", dynamic=True)


def test_query_rejects_conversion_and_format() -> None:
    with pytest.raises(TypeError):
        compile_template(t"{X=}", dynamic=False)
    with pytest.raises(TypeError):
        compile_template(t"{X!r}", dynamic=False)
    with pytest.raises(TypeError):
        compile_template(t"{X:>3}", dynamic=False)
    with pytest.raises(TypeError):
        compile_template(t"{'a':ident}", dynamic=False)


def test_query_rejects_nested_template() -> None:
    inner = t"1"
    with pytest.raises(TypeError):
        compile_template(t"{inner}", dynamic=False)


def test_dynamic_rejects_other_specs() -> None:
    with pytest.raises(TypeError):
        compile_template(t"{X!s}", dynamic=True)
    with pytest.raises(TypeError):
        compile_template(t"{X:raw}", dynamic=True)
    with pytest.raises(TypeError):
        compile_template(t"{X=}", dynamic=True)


def test_non_template_argument() -> None:
    with pytest.raises(TypeError):
        compile_template("SELECT 1", dynamic=False)  # ty: ignore[invalid-argument-type]


PLACEMENT_ERROR: Final[str] = "an ident interpolation must not be inside a string literal or comment"


# the overrides return values that would change the SQL if the compiler ever called them
Hostile = type(
    "Hostile",
    (str,),
    {
        "__slots__": (),
        "__contains__": lambda *_, **__: False,
        "__len__": lambda *_, **__: 1,
        "encode": lambda *_, **__: b"evil",
        "replace": lambda *_, **__: "evil",
        "__iter__": lambda *_, **__: iter("evil"),
        "__add__": lambda *_, **__: "evil",
        "__radd__": lambda *_, **__: "evil",
        "__format__": lambda *_, **__: "evil",
        "__repr__": lambda *_, **__: "evil",
    },
)
HostileTuple = type(
    "HostileTuple",
    (tuple,),
    {
        "__iter__": lambda *_, **__: iter(("evil",)),
        "__len__": lambda *_, **__: 0,
        "__getitem__": lambda *_, **__: "evil",
    },
)
ClaimsToBeTemplate = type("ClaimsToBeTemplate", (), {"__class__": property(lambda *_, **__: Template)})


def sql_of(t: Template) -> str:
    return compile_template(t, dynamic=True)[0].sql


def test_ident_ignores_str_subclass_overrides() -> None:
    name = Hostile('a"b')
    assert sql_of(t"SELECT {name:ident}") == 'SELECT "a""b"'


def test_ident_ignores_tuple_subclass_overrides() -> None:
    parts = HostileTuple(("s", Hostile("t")))
    assert sql_of(t"SELECT {parts:ident}") == 'SELECT "s"."t"'


def test_ident_error_messages_do_not_use_the_value() -> None:
    long = Hostile("a" * 64)
    with pytest.raises(TypeError) as caught:
        compile_template(t"{long:ident}", dynamic=True)
    assert "evil" not in str(caught.value)


@pytest.mark.parametrize("name", ["\x00", "\x1f", "\x7f", "\u0085", "\u009f", "a\nb", "a\x00"])
def test_ident_rejects_control_characters(name: str) -> None:
    with pytest.raises(TypeError, match=r"control character|NUL"):
        compile_template(t"{name:ident}", dynamic=True)


@pytest.mark.parametrize("name", ["é", "日本語", "naïve", "\u00a0", "\u2028", "😀"])
def test_ident_accepts_multibyte_text(name: str) -> None:
    assert sql_of(t"{name:ident}") == f'"{name}"'


def test_ident_rejects_a_lone_surrogate() -> None:
    name = "a\ud800"
    with pytest.raises(TypeError):
        compile_template(t"{name:ident}", dynamic=True)
    parts = ("ok", "\udfff")
    with pytest.raises(TypeError):
        compile_template(t"{parts:ident}", dynamic=True)


def test_ident_byte_limit_counts_utf8_bytes() -> None:
    ok, bad = "é" * 31 + "a", "é" * 32
    assert sql_of(t"{ok:ident}") == f'"{ok}"'
    with pytest.raises(TypeError):
        compile_template(t"{bad:ident}", dynamic=True)


def test_nul_in_static_text_is_rejected() -> None:
    with pytest.raises(TypeError, match="SQL text must not contain a NUL character"):
        compile_template(Template("SELECT '\x00'"), dynamic=False)
    with pytest.raises(TypeError, match="SQL text must not contain a NUL character"):
        compile_template(Template("SELECT '\x00'"), dynamic=True)


def test_nul_in_a_nested_template_is_rejected() -> None:
    inner = Template("a\x00b")
    with pytest.raises(TypeError, match="SQL text must not contain a NUL character"):
        compile_template(t"SELECT {inner}", dynamic=True)


def nested(count: int) -> Template:
    args: list[str | Interpolation[object]] = [Interpolation(i, "i") for i in range(count)]
    return Template(*args)


def test_parameter_limit_across_nested_templates() -> None:
    half = nested(MAX_PARAMETERS // 2)
    rest = nested(MAX_PARAMETERS - MAX_PARAMETERS // 2)
    _, values = compile_template(t"{half} {rest}", dynamic=True)
    assert len(values) == MAX_PARAMETERS
    one_more = nested(MAX_PARAMETERS - MAX_PARAMETERS // 2 + 1)
    with pytest.raises(TypeError, match=r"a query takes at most 65535 parameters, got 65536"):
        compile_template(t"{half} {one_more}", dynamic=True)


def test_parameter_limit_applies_to_query() -> None:
    assert len(compile_template(nested(MAX_PARAMETERS), dynamic=False)[1]) == MAX_PARAMETERS
    with pytest.raises(TypeError, match="got 65536"):
        compile_template(nested(MAX_PARAMETERS + 1), dynamic=False)


def test_an_object_that_claims_to_be_a_template_is_bound() -> None:
    fake = ClaimsToBeTemplate()
    assert isinstance(fake, Template)
    for dynamic in (False, True):
        compiled, values = compile_template(t"SELECT {fake}", dynamic=dynamic)
        assert compiled.sql == "SELECT $1"
        assert values == [fake]


def test_an_object_that_claims_to_be_a_template_is_not_a_valid_argument() -> None:
    with pytest.raises(TypeError):
        compile_template(ClaimsToBeTemplate(), dynamic=True)  # ty: ignore[invalid-argument-type]


EqualStr = type(
    "EqualStr",
    (str,),
    {
        "__slots__": (),
        "__eq__": lambda *_, **__: True,
        "__hash__": lambda *_, **__: 0,
        "__bool__": lambda *_, **__: False,
    },
)


@pytest.mark.parametrize("spec", [EqualStr("ident"), EqualStr(""), EqualStr("x")])
def test_a_str_subclass_format_spec_is_rejected(spec: str) -> None:
    item = Interpolation("t", "x", None, spec)
    for dynamic in (False, True):
        with pytest.raises(TypeError, match="format spec must be a str"):
            compile_template(Template(item), dynamic=dynamic)


@pytest.mark.parametrize("conversion", [EqualStr("r"), EqualStr("s"), EqualStr("a")])
def test_a_str_subclass_conversion_is_rejected(conversion: str) -> None:
    item = Interpolation("t", "x", conversion, "")  # ty: ignore[invalid-argument-type]
    for dynamic in (False, True):
        with pytest.raises(TypeError, match="conversion must be a str or None"):
            compile_template(Template(item), dynamic=dynamic)


def test_a_str_subclass_expression_is_not_formatted() -> None:
    item = Interpolation("t", Hostile("x"), None, "bad")
    with pytest.raises(TypeError) as caught:
        compile_template(Template(item), dynamic=True)
    assert "evil" not in str(caught.value)


PLACEMENT_REJECTED: Final[list[Template]] = [
    t"SELECT 'a {'n':ident} b'",
    t"SELECT 'it''s {'n':ident}'",
    t"SELECT E'a\\' {'n':ident}'",
    t"SELECT e'x {'n':ident}'",
    t"SELECT U&'x {'n':ident}'",
    t"SELECT B'1 {'n':ident}'",
    t"SELECT X'1 {'n':ident}'",
    t"SELECT $$ {'n':ident} $$",
    t"SELECT $q$ $$ {'n':ident} $q$",
    t"SELECT $q1$ {'n':ident} $q1$",
    t'SELECT "a {"n":ident} b"',
    t'SELECT "a"" {"n":ident}"',
    t"SELECT /* {'n':ident} */ 1",
    t"SELECT /* /* */ {'n':ident} */ 1",
    t"SELECT 1 -- {'n':ident}",
    t"SELECT 1 -- x\n'{'n':ident}'",
]
PLACEMENT_ACCEPTED: Final[list[tuple[Template, str]]] = [
    (t"SELECT 'a' || {'n':ident}", "SELECT 'a' || \"n\""),
    (t"SELECT 'it''s' || {'n':ident}", "SELECT 'it''s' || \"n\""),
    (t"SELECT E'a\\'b' || {'n':ident}", "SELECT E'a\\'b' || \"n\""),
    (t"SELECT 'a\\' || {'n':ident}", "SELECT 'a\\' || \"n\""),
    (t"SELECT $$a$$ || {'n':ident}", 'SELECT $$a$$ || "n"'),
    (t"SELECT $q$a$$b$q$ || {'n':ident}", 'SELECT $q$a$$b$q$ || "n"'),
    (t'SELECT "a""b" FROM {"n":ident}', 'SELECT "a""b" FROM "n"'),
    (t"SELECT /* a */ {'n':ident}", 'SELECT /* a */ "n"'),
    (t"SELECT /* /* a */ b */ {'n':ident}", 'SELECT /* /* a */ b */ "n"'),
    (t"SELECT 1 -- x\n{'n':ident}", 'SELECT 1 -- x\n"n"'),
    (t"SELECT 1 -- x\r{'n':ident}", 'SELECT 1 -- x\r"n"'),
    (t"SELECT 1 /* - */ - {'n':ident}", 'SELECT 1 /* - */ - "n"'),
    (t"SELECT a$$b, {'n':ident}", 'SELECT a$$b, "n"'),
    (t"SELECT x$q$, {'n':ident}", 'SELECT x$q$, "n"'),
    (t"SELECT bit'1', {'n':ident}", "SELECT bit'1', \"n\""),
    (t"SELECT xe'a', {'n':ident}", "SELECT xe'a', \"n\""),
]


@pytest.mark.parametrize("template", PLACEMENT_REJECTED)
def test_ident_inside_a_literal_or_comment_is_rejected(template: Template) -> None:
    with pytest.raises(TypeError, match=PLACEMENT_ERROR):
        compile_template(template, dynamic=True)


@pytest.mark.parametrize(("template", "sql"), PLACEMENT_ACCEPTED)
def test_ident_in_code_position_passes(template: Template, sql: str) -> None:
    assert compile_template(template, dynamic=True)[0].sql == sql


def test_placement_state_is_carried_across_a_nested_template() -> None:
    open_string = t"'abc "
    open_comment = t"/* "
    open_dollar = t"$q$ "
    open_line = t"-- "
    for opener in (open_string, open_comment, open_dollar, open_line):
        with pytest.raises(TypeError, match=PLACEMENT_ERROR):
            compile_template(t"SELECT {opener} {'n':ident}", dynamic=True)
        with pytest.raises(TypeError, match=PLACEMENT_ERROR):
            compile_template(t"SELECT {t'{opener}'} x {'n':ident}", dynamic=True)
    closed = t"'abc' "
    assert sql_of(t"SELECT {closed}{'n':ident}") == "SELECT 'abc' \"n\""


def test_placement_is_decided_on_the_flattened_text() -> None:
    empty = Template("")
    with pytest.raises(TypeError, match=PLACEMENT_ERROR):
        compile_template(t"SELECT /{empty}* {'n':ident} */", dynamic=True)
    with pytest.raises(TypeError, match=PLACEMENT_ERROR):
        compile_template(t"SELECT -{empty}- {'n':ident}", dynamic=True)


def test_placement_state_survives_a_placeholder_and_earlier_idents() -> None:
    assert sql_of(t"SELECT {'a':ident}, {X}, {'b':ident}") == 'SELECT "a", $1, "b"'
    with pytest.raises(TypeError, match=PLACEMENT_ERROR):
        compile_template(t"SELECT {'a':ident}, '{X}', '{'b':ident}'", dynamic=True)
    with pytest.raises(TypeError, match=PLACEMENT_ERROR):
        compile_template(t"SELECT {'a':ident} /* {X} {'b':ident} */", dynamic=True)


def test_numbered_placeholders_are_not_dollar_quotes() -> None:
    sql = sql_of(t"SELECT {X}, {Y}, {X} FROM {'n':ident} WHERE a = $1 AND b = $2$ AND c = {'m':ident}")
    assert sql.startswith("SELECT $1, $2, $3 FROM")
    assert sql_of(t"SELECT {X} + $1 + {'n':ident}") == 'SELECT $1 + $1 + "n"'
    assert sql_of(t"SELECT $1$ {'n':ident}") == 'SELECT $1$ "n"'
    assert sql_of(t"SELECT {X}$$a$$ || {'n':ident}") == 'SELECT $1 $$a$$ || "n"'


def test_a_placeholder_never_closes_a_dollar_quote() -> None:
    with pytest.raises(TypeError, match=PLACEMENT_ERROR):
        compile_template(t"SELECT $$ x ${X} {'n':ident}", dynamic=True)
