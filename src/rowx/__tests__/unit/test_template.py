import pytest

from rowx._template import Compiled, compile_parts, compile_template

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
