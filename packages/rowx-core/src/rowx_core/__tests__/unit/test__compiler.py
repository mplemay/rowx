import pytest
from rowx_core import capture_template
from rowx_core._compiler import Dialect, compile_parts, compile_plan, quote_ident, quote_part


def sqlite_ends_in_code(text: str) -> bool:
    # a toy lexer: only single-quoted strings
    return text.count("'") % 2 == 0


def sqlite_quote(value: object) -> str:
    return quote_ident(value, 10)


# `?N` placeholders: a digit after one would extend it, but nothing before it can fuse with the `?`
SQLITE = Dialect(
    placeholder="?{}".format,
    pads_before_param=lambda _ch: False,
    pads_after_param=str.isdigit,
    quote_ident=sqlite_quote,
    ends_in_code=sqlite_ends_in_code,
)


def test_placeholder_format_comes_from_the_dialect() -> None:
    compiled, values = compile_plan(capture_template(t"select {1}, {2} from t", dynamic=False), SQLITE)
    assert compiled.sql == "select ?1, ?2 from t"
    assert values == [1, 2]
    assert [compiled.sql[a:b] for a, b in compiled.placeholders] == ["?1", "?2"]
    assert [compiled.sql[a:b] for a, b in compiled.segments] == ["select ", ", ", " from t"]


def test_padding_follows_the_dialect_rule() -> None:
    # nothing fuses before a `?N`, so no space is added there
    compiled, _ = compile_plan(capture_template(t"x{1}", dynamic=False), SQLITE)
    assert compiled.sql == "x?1"
    assert compiled.padding == ((0, 0), (0, 0))
    # a digit after it would extend the placeholder
    compiled, _ = compile_plan(capture_template(t"{1}2", dynamic=False), SQLITE)
    assert compiled.sql == "?1 2"
    assert compiled.padding == ((0, 0), (1, 0))
    compiled, _ = compile_plan(capture_template(t"{1}x", dynamic=False), SQLITE)
    assert compiled.sql == "?1x"
    assert compiled.padding == ((0, 0), (0, 0))


def test_padding_before_a_placeholder_is_reported() -> None:
    dialect = Dialect(
        placeholder="${}".format,
        pads_before_param=str.isalnum,
        pads_after_param=str.isalnum,
        quote_ident=sqlite_quote,
        ends_in_code=sqlite_ends_in_code,
    )
    compiled, _ = compile_plan(capture_template(t"x{1}y", dynamic=False), dialect)
    assert compiled.sql == "x $1 y"
    assert compiled.padding == ((0, 1), (1, 0))


def test_compile_parts_uses_the_dialect_placeholder() -> None:
    compiled = compile_parts(["a = ", " and b = ", ""], SQLITE)
    assert compiled.sql == "a = ?1 and b = ?2"
    assert len(compiled.placeholders) == 2


def test_identifier_rule_comes_from_the_dialect() -> None:
    compiled, _ = compile_plan(capture_template(t"select * from {('main', 'tbl'):ident}", dynamic=True), SQLITE)
    assert compiled.sql == 'select * from "main"."tbl"'
    with pytest.raises(TypeError, match="identifier exceeds 10 bytes"):
        compile_plan(capture_template(t"select * from {'x' * 11:ident}", dynamic=True), SQLITE)


def test_ident_is_refused_inside_a_string_by_the_dialect_lexer() -> None:
    with pytest.raises(TypeError, match="an ident interpolation must not be inside a string literal or comment"):
        compile_plan(capture_template(t"select 'a {'t':ident}", dynamic=True), SQLITE)


def test_adjacent_quoted_identifiers_are_separated() -> None:
    compiled, _ = compile_plan(capture_template(t"{'a':ident}{'b':ident}", dynamic=True), SQLITE)
    assert compiled.sql == '"a" "b"'


def test_the_parameter_ceiling_is_optional() -> None:
    values = list(range(5))
    plan = capture_template(t"{values[0]}{values[1]}{values[2]}{values[3]}{values[4]}", dynamic=False)
    compiled, params = compile_plan(plan, SQLITE)
    assert len(params) == 5
    assert compiled.sql == "?1?2?3?4?5"
    capped = Dialect(
        placeholder=SQLITE.placeholder,
        pads_before_param=SQLITE.pads_before_param,
        pads_after_param=SQLITE.pads_after_param,
        quote_ident=SQLITE.quote_ident,
        ends_in_code=SQLITE.ends_in_code,
        max_parameters=4,
    )
    with pytest.raises(TypeError, match=r"a query takes at most 4 parameters, got 5"):
        compile_plan(plan, capped)


def test_quote_part_checks_in_order() -> None:
    assert quote_part('a"b', 63) == '"a""b"'
    assert quote_part("é" * 3, 6) == '"ééé"'
    with pytest.raises(TypeError, match="identifier exceeds 5 bytes"):
        quote_part("é" * 3, 5)
    with pytest.raises(TypeError, match="identifier must be a str, got int"):
        quote_part(1, 63)
    with pytest.raises(TypeError, match="identifier must not be empty"):
        quote_part("", 63)
    with pytest.raises(TypeError, match="identifier must be valid UTF-8 text"):
        quote_part("\ud800", 63)
    with pytest.raises(TypeError, match="identifier must not contain a NUL character"):
        quote_part("a\x00", 63)
    with pytest.raises(TypeError, match="identifier must not contain a control character"):
        quote_part("a\n", 63)


def test_quote_ident_joins_a_tuple_and_applies_the_limit_to_each_part() -> None:
    assert quote_ident(("s", "t"), 3) == '"s"."t"'
    with pytest.raises(TypeError, match="identifier tuple must not be empty"):
        quote_ident((), 3)
    with pytest.raises(TypeError, match="identifier exceeds 3 bytes"):
        quote_ident(("abc", "abcd"), 3)
