import os
import re
import secrets
import sqlite3
from random import Random
from typing import Final

import pytest
from rowx_core._compiler import compile_parts as compile_parts_impl
from rowx_sqlite._compiler import (
    COMPILER,
    DIALECT,
    Compiled,
    StatementFacts,
    compile_parts,
    compile_template,
    parameter_spans,
    statement_facts,
)

X: Final[int] = 1
Y: Final[int] = 2

DEFAULT_SEED: Final[int] = 357
DEFAULT_COUNT: Final[int] = 1000
SEED_ENV: Final[str] = "ROWX_FUZZ_SEED"
COUNT_ENV: Final[str] = "ROWX_FUZZ_COUNT"


def check(c: Compiled, sql: str, params: list[str], segments: list[str]) -> None:
    assert c.sql == sql
    assert [c.sql[a:b] for a, b in c.placeholders] == params
    assert [c.sql[a:b] for a, b in c.segments] == segments
    assert len(c.segments) == len(c.placeholders) + 1


def facts(sql: str) -> StatementFacts:
    return statement_facts(sql)


def test_placeholders_are_dense_question_numbers() -> None:
    c, values = compile_template(t"{X}{X}", dynamic=False)
    check(c, "?1 ?2", ["?1", "?2"], ["", " ", ""])
    assert values == [1, 1]
    assert COMPILER.compile_parts(["a", "b"]).sql == "a ?1 b"


def test_adjacency_after_identifier() -> None:
    c, _ = compile_template(t"SELECT 1 AS x{Y}", dynamic=False)
    check(c, "SELECT 1 AS x ?1", ["?1"], ["SELECT 1 AS x ", ""])


def test_adjacency_before_digit_and_identifier() -> None:
    c, _ = compile_template(t"{X}1", dynamic=False)
    check(c, "?1 1", ["?1"], ["", " 1"])
    c, _ = compile_template(t"{X}x", dynamic=False)
    check(c, "?1 x", ["?1"], ["", " x"])
    c, _ = compile_template(t"{X}é", dynamic=False)
    assert c.sql == "?1 é"


def test_no_space_next_to_punctuation() -> None:
    c, _ = compile_template(t"a = ({X}, {Y})", dynamic=False)
    check(c, "a = (?1, ?2)", ["?1", "?2"], ["a = (", ", ", ")"])


def test_non_ascii_is_identifier_char() -> None:
    c, _ = compile_template(t"é{X}", dynamic=False)
    assert c.sql == "é ?1"


def test_padding_offsets() -> None:
    c = compile_parts(["a", "b", "c"])
    assert c.sql == "a ?1 b ?2 c"
    assert c.padding == ((0, 1), (1, 1), (1, 0))
    c = compile_parts(["a = ", ", ", ""])
    assert c.padding == ((0, 0), (0, 0), (0, 0))
    c = compile_parts(["", "9"])
    assert c.sql == "?1 9"
    assert c.padding == ((0, 0), (1, 0))


def test_nested_flatten_and_renumber() -> None:
    inner = t"b = {Y}"
    c, values = compile_template(t"a = {X} AND {inner} AND c = {X}", dynamic=True)
    assert c.sql == "a = ?1 AND b = ?2 AND c = ?3"
    assert values == [1, 2, 1]


def test_no_parameter_ceiling() -> None:
    assert DIALECT.max_parameters is None
    c = compile_parts([""] * 70000)
    assert len(c.placeholders) == 69999
    assert c.sql[c.placeholders[-1][0] : c.placeholders[-1][1]] == "?69999"


def test_ident_single_and_tuple() -> None:
    s, t_ = "s", "t"
    c, values = compile_template(t"SELECT * FROM {(s, t_):ident}", dynamic=True)
    assert c.sql == 'SELECT * FROM "s"."t"'
    assert values == []
    c, _ = compile_template(t"SELECT {'a"b':ident}", dynamic=True)
    assert c.sql == 'SELECT "a""b"'
    long = "a" * 200
    c, _ = compile_template(t"{long:ident}", dynamic=True)
    assert c.sql == f'"{long}"'


def test_ident_rejects_control_characters() -> None:
    bad = "a\x01"
    with pytest.raises(TypeError, match="control"):
        compile_template(t"{bad:ident}", dynamic=True)


def test_ident_inside_string_or_comment_is_rejected() -> None:
    name = "t"
    with pytest.raises(TypeError, match="ident"):
        compile_template(t"SELECT '{name:ident}'", dynamic=True)
    with pytest.raises(TypeError, match="ident"):
        compile_template(t"SELECT 1 -- {name:ident}", dynamic=True)


@pytest.mark.parametrize(
    "sql",
    ["SELECT ?", "SELECT ?1", "SELECT ?0", "SELECT :x", "SELECT @x", "SELECT $x", "SELECT $1", "SELECT #x", "SELECT $"],
)
def test_static_sigils_are_rejected(sql: str) -> None:
    with pytest.raises(TypeError, match="interpolation"):
        compile_parts([sql])
    with pytest.raises(TypeError, match="interpolation"):
        compile_parts([sql, ""])


def test_static_sigil_next_to_interpolation_is_rejected() -> None:
    with pytest.raises(TypeError, match="interpolation"):
        compile_template(t"SELECT ?1, {X}", dynamic=False)


def test_dollar_is_an_identifier_character_inside_a_name() -> None:
    c, _ = compile_template(t"SELECT foo$bar, a$1, {X} FROM t$x", dynamic=False)
    assert c.sql == "SELECT foo$bar, a$1, ?1 FROM t$x"
    assert parameter_spans("SELECT foo$bar, a$1") == ()
    assert parameter_spans("SELECT 1 AS $x") == ((12, 14),)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT '?1 :x @y $z #w'",
        "SELECT 'it''s ?'",
        "SELECT X'0?'",
        'SELECT "a ?1"',
        'SELECT "a"" ?1"',
        "SELECT `a ?1`",
        "SELECT `a`` :x`",
        "SELECT [a ?1]",
        "SELECT [a\\] FROM t -- ?",
        "SELECT 1 -- ?1 :x\n",
        "SELECT 1 /* ?1 */",
        "SELECT 1 /* /* nested ?1 */",
        "SELECT 1 -- ?\r",
    ],
)
def test_sigils_in_quoted_text_and_comments_are_accepted(sql: str) -> None:
    c = compile_parts([sql])
    assert c.sql == sql
    assert not c.placeholders


def test_block_comments_do_not_nest() -> None:
    # the first `*/` closes the comment, so the `?1` after it is live
    assert parameter_spans("/* /* */ ?1 */") == ((9, 11),)


def test_bracket_identifiers_end_at_first_bracket_with_no_escape() -> None:
    assert parameter_spans("[a]]?1") == ((4, 6),)


def test_doubled_quotes_do_not_end_the_token() -> None:
    assert parameter_spans("'a''?1' ?2") == ((8, 10),)
    assert parameter_spans('"a""?1" ?2') == ((8, 10),)


def test_no_dollar_quotes_or_escape_strings() -> None:
    # `$$` is a (rejected) variable, and `\'` does not escape inside E'...'
    assert parameter_spans("SELECT $$a$$") != ()
    assert parameter_spans("SELECT E'\\' ?1") == ((12, 14),)


def test_variable_token_extents() -> None:
    assert parameter_spans(":a::b ?12x") == ((0, 5), (6, 9))
    assert parameter_spans("$a(b) @é#") == ((0, 5), (6, 8), (8, 9))


@pytest.mark.parametrize(
    "sql",
    [
        "'",
        "'a''",
        '"a',
        "`a",
        "[a",
        "-- a",
        "/* a",
        "/* a *",
        "SELECT 1 -- a ?1",
    ],
)
def test_ends_in_code_false_inside_strings_identifiers_and_comments(sql: str) -> None:
    assert not DIALECT.ends_in_code(sql)


@pytest.mark.parametrize(
    "sql",
    ["", "SELECT 1", "'a'", "'a''b'", '"a"', "`a`", "[a]", "-- a\n", "/* a */", "SELECT 1 -- a\nFROM t", "x'ab'"],
)
def test_ends_in_code_true_in_code(sql: str) -> None:
    assert DIALECT.ends_in_code(sql)


def test_interpolation_inside_a_string_or_comment_is_rejected() -> None:
    with pytest.raises(TypeError, match="inside a string"):
        compile_template(t"SELECT '{X}'", dynamic=False)
    with pytest.raises(TypeError, match="inside a string"):
        compile_template(t"SELECT 1 /* {X} */", dynamic=False)
    with pytest.raises(TypeError, match="inside a string"):
        compile_parts(["SELECT 1 -- ", ""])
    with pytest.raises(TypeError, match="inside a string"):
        compile_parts(["SELECT '", "'"])


def test_lexing_is_linear() -> None:
    for sql in ["'" * 200_000, "/*" * 200_000, "$" * 200_000, "a" * 200_000, "[" * 200_000, "1." * 100_000]:
        statement_facts(sql)
        parameter_spans(sql)


def test_facts_simple_select_has_only_base_tables() -> None:
    f = facts('SELECT a FROM t JOIN "S"."U" AS u ON t.a = u.a, [v w] x INNER JOIN y CROSS JOIN z')
    assert f.sources == (("t",), ("s", "u"), ("v w",), ("y",), ("z",))
    assert not (f.cte_names)
    assert not (f.has_subquery)
    assert not (f.has_compound)
    assert not (f.has_outer_join)


def test_facts_names_fold_ascii_only_and_unquote() -> None:
    f = facts('SELECT 1 FROM `Ab`, "É""x", Main.T')
    assert f.sources == (("ab",), ('É"x',), ("main", "t"))


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 FROM a LEFT JOIN b ON 1",
        "SELECT 1 FROM a LEFT OUTER JOIN b ON 1",
        "SELECT 1 FROM a RIGHT JOIN b ON 1",
        "SELECT 1 FROM a FULL OUTER JOIN b ON 1",
        "SELECT 1 FROM a NATURAL JOIN b",
        "SELECT 1 FROM a NATURAL LEFT JOIN b",
        "SELECT 1 FROM (a JOIN b ON 1 LEFT JOIN c ON 1)",
    ],
)
def test_facts_outer_joins(sql: str) -> None:
    assert facts(sql).has_outer_join


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT left(a, 1), right(a, 1) FROM t",
        "SELECT 1 FROM a JOIN b ON 1 CROSS JOIN c INNER JOIN d ON 1",
        "SELECT 'left join' FROM a",
        "SELECT 1 FROM a -- left join b\n",
    ],
)
def test_facts_no_outer_join(sql: str) -> None:
    assert not facts(sql).has_outer_join


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 UNION SELECT 2",
        "SELECT 1 UNION ALL SELECT 2",
        "SELECT 1 INTERSECT SELECT 2",
        "SELECT 1 EXCEPT SELECT 2",
        "SELECT * FROM (SELECT 1 UNION SELECT 2)",
    ],
)
def test_facts_compound(sql: str) -> None:
    assert facts(sql).has_compound


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT (SELECT 1)",
        "SELECT 1 FROM t WHERE a IN (SELECT 1)",
        "SELECT 1 WHERE EXISTS (SELECT 1)",
        "SELECT * FROM (SELECT 1) AS s",
        "SELECT * FROM (VALUES (1))",
        "SELECT * FROM ( SELECT 1 )",
        "SELECT * FROM (/* c */ SELECT 1)",
    ],
)
def test_facts_subquery(sql: str) -> None:
    assert facts(sql).has_subquery


def test_facts_parenthesised_join_group_is_not_a_subquery() -> None:
    f = facts("SELECT 1 FROM (a JOIN b ON 1), c")
    assert not f.has_subquery
    assert f.sources == (("a",), ("b",), ("c",))


def test_facts_cte_names_including_shadowing() -> None:
    f = facts(
        "WITH RECURSIVE T(x) AS (SELECT 1), u AS MATERIALIZED (SELECT 2), w AS NOT MATERIALIZED (SELECT 3) SELECT * FROM t, u, w"
    )
    assert f.cte_names == {"t", "u", "w"}
    assert f.sources == (("t",), ("u",), ("w",))
    assert f.has_subquery


def test_facts_sources_inside_subqueries_and_after_them() -> None:
    f = facts("SELECT 1 FROM (SELECT 1 FROM a WHERE x IN (SELECT 1 FROM b)) s JOIN c ON 1 WHERE 1 ORDER BY 1")
    assert f.sources == (("a",), ("b",), ("c",))


def test_facts_commas_outside_from_are_not_sources() -> None:
    f = facts("SELECT a, b, f(c, d) FROM t WHERE x IN (1, 2) GROUP BY a, b ORDER BY a, b LIMIT 1, 2")
    assert f.sources == (("t",),)


def test_facts_table_valued_function_is_a_source() -> None:
    f = facts("SELECT value FROM json_each('[1]') AS j, t")
    assert f.sources == (("json_each",), ("t",))


def test_facts_is_distinct_from_is_not_a_from_clause() -> None:
    f = facts("SELECT a IS NOT DISTINCT FROM b FROM t")
    assert f.sources == (("t",),)


def test_facts_dml_sources() -> None:
    assert facts("DELETE FROM t WHERE 1").sources == (("t",),)
    assert facts("UPDATE t SET a = 1 FROM u WHERE 1").sources == (("u",),)


def test_facts_ignore_strings_and_comments() -> None:
    f = facts("SELECT 'from x', \"from\" /* from y */ FROM t -- join z\n")
    assert f.sources == (("t",),)


def test_facts_tolerate_unbalanced_input() -> None:
    assert facts("SELECT 1 FROM a ) ) JOIN b").sources == (("a",), ("b",))
    assert facts("SELECT (((").sources == ()


def fuzz_settings() -> tuple[int, int]:
    raw = os.environ.get(SEED_ENV, str(DEFAULT_SEED))
    seed = secrets.randbelow(2**31) if raw == "random" else int(raw)
    return seed, int(os.environ.get(COUNT_ENV, DEFAULT_COUNT))


HOLE: Final[object] = object()
COLUMNS: Final[str] = 'CREATE TABLE t (a, b, c, "x ?1", "y :z", "it""s $q", "p @r", "w #v", "k]", "l`m", "[n")'
CODE_ATOMS: Final[tuple[str, ...]] = (
    "1",
    "a",
    "b",
    "t.a",
    "foo$bar",
    "a$1",
    "'s'",
    "'?1'",
    "'it''s :x'",
    '"x ?1"',
    '"y :z"',
    '"it""s $q"',
    "`x ?1`",
    "`l``m`",
    "[x ?1]",
    "[y :z]",
    "[k]]",
    "[w #v]",
    "x'0a'",
    "?",
    "?1",
    "?2",
    "?12",
    "?0",
    ":n",
    ":n1",
    "@n",
    "$n",
    "#n",
    "$a$b",
    ":é",
    "$1",
    "$",
    ":",
    "@",
    "#",
    ":a::b",
    "$a(b)",
    "$a(b c)",
    "1.5",
    "0x1F",
    "1e5",
    "'a' || 'b'",
    "a + 1",
)
WRAPPED_ATOMS: Final[tuple[tuple[object, ...], ...]] = (
    ("'x ", HOLE, " y'"),
    ("'", HOLE, "'"),
    ("/* ", HOLE, " */"),
    ("/* ?1 */ ", HOLE),
    ("1 -- ", HOLE, "\n"),
    ("1 -- ?1\n, ", HOLE),
    ('"q ', HOLE, '"'),
    ("[q ", HOLE, "]"),
    ("`q ", HOLE, "`"),
)
JOINERS: Final[tuple[str, ...]] = (", ", " + ", " || ", ",", " ", "", "/**/", " -- c\n+")


def generate(rng: Random) -> list[str]:
    parts: list[object] = ["SELECT "]
    for index in range(rng.randint(1, 5)):
        if index:
            parts.append(rng.choice(JOINERS))
        roll = rng.random()
        if roll < 0.35:
            parts.append(HOLE)
        elif roll < 0.5:
            parts.extend(rng.choice(WRAPPED_ATOMS))
        else:
            parts.append(rng.choice(CODE_ATOMS))
    if rng.random() < 0.6:
        parts.append(" FROM t")
    if rng.random() < 0.15:
        parts.append(rng.choice((" /* ?1", " -- :x", " /* ", " 'x")))
    strings = [""]
    for part in parts:
        if part is HOLE:
            strings.append("")
        else:
            assert isinstance(part, str)
            strings[-1] += part
    return strings


def new_database() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.execute(COLUMNS)
    return db


USES: Final[re.Pattern[str]] = re.compile(r"uses (\d+)")
MISSING: Final[re.Pattern[str]] = re.compile(r"binding parameter (\S+?)\.")


# How many parameters SQLite's own tokenizer finds, or None when the statement does not prepare. Preparing
# with no bindings fails with "The current statement uses N", which is sqlite3_bind_parameter_count.
def sqlite_count(db: sqlite3.Connection, sql: str) -> int | None:
    try:
        db.execute(sql)
    except sqlite3.ProgrammingError as e:
        return int(USES.search(str(e))[1])  # ty: ignore[not-subscriptable]
    except sqlite3.Error:
        return None
    return 0


# SQLite's parameter names, discovered by binding a dict that grows with each "You did not supply a value" error
def sqlite_names(db: sqlite3.Connection, sql: str) -> list[str] | None:
    names: dict[str, None] = {}
    while True:
        try:
            db.execute(sql, names)
        except sqlite3.ProgrammingError as e:
            if not (m := MISSING.search(str(e))):
                return None
            if (key := m[1][1:]) in names:
                return None
            names[key] = None
            continue
        except sqlite3.Error:
            return None
        return list(names)


# sqlite3_bind_parameter_count for the spans the lexer found: `?` and new names take the next index, `?N` takes N
def lexer_count(sql: str) -> int:
    count = 0
    seen: set[str] = set()
    for start, end in parameter_spans(sql):
        text = sql[start:end]
        if text == "?":
            count += 1
        elif text[0] == "?":
            count = max(count, int(text[1:]))
        elif text not in seen:
            seen.add(text)
            count += 1
    return count


def replace_spans(sql: str, spans: tuple[tuple[int, int], ...]) -> str:
    out = sql
    for start, end in reversed(spans):
        out = out[:start] + "NULL" + out[end:]
    return out


def differential(db: sqlite3.Connection, strings: list[str]) -> bool | None:
    compiled = compile_parts_impl(strings, DIALECT)
    sql = compiled.sql
    holes = len(compiled.placeholders)
    total = sqlite_count(db, sql)
    # the statement with its own placeholders blanked leaves only what SQLite reads as a parameter in static text
    leftover = sqlite_count(db, replace_spans(sql, compiled.placeholders))
    if total is None or leftover is None:
        return None
    assert lexer_count(sql) == total, f"lexer disagrees with sqlite on {sql!r}"
    # a hole is live when it alone, with the others blanked, makes SQLite count exactly that many parameters
    live = all(
        sqlite_count(db, replace_spans(sql, compiled.placeholders[:k] + compiled.placeholders[k + 1 :])) == k + 1
        for k in range(holes)
    )
    expected = leftover == 0 and live
    try:
        compile_parts(strings)
    except TypeError:
        accepted = False
    else:
        accepted = True
    assert accepted == expected, f"accepted={accepted} but sqlite says leftover={leftover} total={total} holes={holes}"
    names = sqlite_names(db, sql)
    if names is not None and "?" not in sql.replace("'", ""):
        spans = parameter_spans(sql)
        mine = list(dict.fromkeys(sql[a + 1 : b] for a, b in spans if sql[a] != "?"))
        if not any(sql[a] == "?" for a, _ in spans):
            assert mine == names, f"names {mine} vs sqlite {names} on {sql!r}"
    return accepted


def test_randomised_differential_against_sqlite() -> None:
    seed, count = fuzz_settings()
    rng = Random(seed)  # ruff: ignore[suspicious-non-cryptographic-random-usage]
    db = new_database()
    seen = {True: 0, False: 0}
    for _ in range(count):
        strings = generate(rng)
        try:
            outcome = differential(db, strings)
        except AssertionError as e:
            msg = f"seed={seed} strings={strings!r}: {e}"
            raise AssertionError(msg) from None
        if outcome is not None:
            seen[outcome] += 1
    db.close()
    # the generator must keep producing both outcomes and mostly valid statements
    assert seen[True] > count // 20, f"seed={seed}: too few accepted statements {seen}"
    assert seen[False] > count // 20, f"seed={seed}: too few rejected statements {seen}"
    assert sum(seen.values()) > count // 4, f"seed={seed}: too few statements prepared {seen}"
