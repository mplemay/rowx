import json
import tempfile
from typing import TYPE_CHECKING, Any, Final

import pytest
from rowx_core.backend import CheckerBackend
from rowx_sqlite._backend import cast, checker, load, placeholder
from rowx_sqlite._compiler import COMPILER
from rowx_sqlite._core import type_table

from rowx.check.resolve import LEAVES

if TYPE_CHECKING:
    from pathlib import Path

    from rowx_sqlite.__tests__.conftest import CheckProject

HEADER: Final[str] = "from rowx import query, query_unchecked\n\n"


def write(project: CheckProject, source: str) -> None:
    project.write(HEADER + source)


def reports(project: CheckProject, source: str, *args: str, url: str | None = None) -> list[dict[str, Any]]:
    write(project, source)
    code, out, err = project.check("--output-format", "json", *args, url=url)
    assert not err
    found: list[dict[str, Any]] = json.loads(out)
    assert code == (1 if found else 0)
    return found


def position(source: str, needle: str) -> tuple[int, int]:
    text = HEADER + source
    before = text[: text.index(needle)]
    return before.count("\n") + 1, len(before.rpartition("\n")[2]) + 1


def test_the_checker_backend_is_registered() -> None:
    backend = load()
    assert backend.checker_factory is checker
    assert isinstance(backend.checker(), CheckerBackend)


def test_the_checker_backend_describes_sqlite() -> None:
    backend = checker()
    assert backend.compiler is COMPILER
    assert backend.override_issues is None
    assert backend.array_key is None
    assert backend.json_keys == {"json"}
    assert (backend.untyped_params, backend.untyped_columns) == (True, True)
    assert backend.describer == "SQLite"
    assert (backend.param_classes, backend.value_classes, backend.value_leaves) == ({}, {}, {})
    assert (backend.range_leaves, backend.range_classes) == ({}, frozenset())
    assert [placeholder(n) for n in (1, 12)] == ["?1", "?12"]
    assert cast("{x}") == "CAST({x} AS TEXT)"
    assert backend.placeholder is placeholder
    assert backend.cast is cast


def test_the_types_come_from_the_type_table() -> None:
    types = checker().types
    assert set(types) == {info.key for info in type_table()}
    results = {
        "integer": "int",
        "boolean": "bool",
        "decimal": "decimal.Decimal",
        "datetimetz": "datetime.datetime",
        "text": "str",
    }
    assert {key: types[key].py_result for key in results} == results
    assert types["any"].alternatives == {"int", "float", "str", "bytes"}
    assert types["integer"].accepted_params == ("int",)


def test_the_checker_knows_every_type_the_type_table_names() -> None:
    backend = checker()
    assert {info.key for info in type_table()} <= set(backend.types)
    # `json` decodes to any Python value, which no leaf names, so it is the one result that is `object`
    leaves = set(LEAVES.values())
    for key, entry in backend.types.items():
        results = {entry.py_result, *entry.alternatives}
        if key == "json":
            assert entry.py_result == "object"
            continue
        if key == "any":
            # a union of the four storage classes, each of which is a leaf
            assert entry.py_result == "int | float | str | bytes"
            assert entry.alternatives == {"int", "float", "str", "bytes"}
            results = set(entry.alternatives)
        assert results <= leaves, key
    assert backend.array_key is None
    assert backend.override_issues is None
    assert (backend.untyped_params, backend.untyped_columns) == (True, True)
    assert backend.placeholder(3) == "?3"
    assert backend.json_keys == {"json"}


def test_a_passing_project_exits_zero(project: CheckProject) -> None:
    write(
        project,
        'query[tuple[int, str, int]](t"SELECT id, name, age FROM users")\n'
        # the WHERE clause turns the view's left join into an inner join, which proves both columns
        'query[tuple[int, str]](t"SELECT user_id, title FROM user_posts WHERE title = {1}")\n',
    )
    assert project.check() == (0, "", "")
    assert project.check("--output-format", "json") == (0, "[]\n", "")


def test_a_type_mismatch_is_reported(project: CheckProject) -> None:
    source = 'query[tuple[int, int]](t"SELECT id, name FROM users")\n'
    (found,) = reports(project, source)
    assert found["rule"] == "type-mismatch"
    assert found["column_index"] == 1
    assert "code" not in found


def test_a_left_join_view_column_is_unproven(project: CheckProject) -> None:
    source = 'query[tuple[int | None, str]](t"SELECT user_id, title FROM user_posts")\n'
    (found,) = reports(project, source)
    assert found["rule"] == "nullability-mismatch"
    assert "nullability not proven, assumed nullable" in found["notes"]
    assert found["help"] is not None
    assert 'alias the column as "title!"' in found["help"]


@pytest.mark.parametrize(
    "source",
    [
        'query[tuple[int]](t"SELECT count(*) FROM users")\n',
        'query[tuple[int]](t"SELECT count(email) FROM users")\n',
        'query[tuple[int, int]](t"SELECT count(*), 1 FROM users")\n',
        "query[tuple[str, str]](t\"SELECT 'x', CAST(age AS TEXT) FROM users\")\n",
        "query[tuple[str]](t\"SELECT coalesce(email, 'none') FROM users\")\n",
        "query[tuple[str]](t\"SELECT ifnull(email, 'none') FROM users\")\n",
        'query[tuple[int, int]](t"SELECT id, count(*) FROM users GROUP BY id")\n',
        'query[tuple[int]](t"SELECT CASE WHEN age > 1 THEN 1 ELSE 0 END FROM users")\n',
        'query[tuple[int, str | None]](t"SELECT users.id, posts.title FROM users LEFT JOIN posts ON posts.id = users.id")\n',
        "query[tuple[str]](t\"SELECT coalesce(posts.title, 'none') FROM users LEFT JOIN posts ON posts.id = users.id\")\n",
        'query[tuple[int]](t"SELECT age + 1 FROM users")\n',
    ],
)
def test_a_computed_column_passes_without_an_override(project: CheckProject, source: str) -> None:
    assert reports(project, source) == []


@pytest.mark.parametrize(
    "source",
    [
        'query[tuple[int]](t"SELECT sum(age) FROM users")\n',
        'query[tuple[int]](t"SELECT max(age) FROM users")\n',
        'query[tuple[float]](t"SELECT avg(age) FROM users")\n',
        'query[tuple[float]](t"SELECT total(age) FROM users")\n',
        'query[tuple[int]](t"SELECT age / 1 FROM users")\n',
        "query[tuple[str]](t\"SELECT email || 'x' FROM users\")\n",
        'query[tuple[int]](t"SELECT nullif(age, 1) FROM users")\n',
        "query[tuple[str]](t\"SELECT CASE WHEN age > 1 THEN 'x' END FROM users\")\n",
        'query[tuple[int]](t"SELECT count(*) + sum(age) FROM users")\n',
    ],
)
def test_a_computed_column_that_can_be_null_still_needs_optional_or_an_override(
    project: CheckProject, source: str
) -> None:
    (found,) = reports(project, source)
    assert found["rule"] == "nullability-mismatch"
    assert "nullability not proven, assumed nullable" in found["notes"]


def test_an_optional_annotation_is_accepted_for_a_column_that_can_be_null(project: CheckProject) -> None:
    source = (
        'query[tuple[int | None, int | None]](t"SELECT sum(age), max(age) FROM users")\n'
        'query[tuple[int]](t"SELECT sum(age) AS \\"total!\\" FROM users")\n'
    )
    assert reports(project, source) == []


def test_a_join_that_needs_an_automatic_index_is_not_proven(project: CheckProject) -> None:
    source = 'query[tuple[int, str | None]](t"SELECT user_id, title FROM user_posts")\n'
    (found,) = reports(project, source)
    assert found["rule"] == "nullability-mismatch"
    assert "nullability not proven, assumed nullable" in found["notes"]


def test_an_inferred_type_is_checked_against_the_annotation(project: CheckProject) -> None:
    source = 'query[tuple[str]](t"SELECT count(*) FROM users")\n'
    (found,) = reports(project, source)
    assert found["rule"] == "type-mismatch"


def test_a_computed_column_that_was_proven_is_reported_when_annotated_optional(project: CheckProject) -> None:
    source = 'query[tuple[int | None]](t"SELECT count(*) FROM users")\n'
    (found,) = reports(project, source)
    assert found["rule"] == "nullability-mismatch"


def test_a_not_null_column_read_as_optional_is_a_mismatch(project: CheckProject) -> None:
    (found,) = reports(project, 'query[tuple[int, str | None]](t"SELECT id, name FROM users")\n')
    assert found["rule"] == "nullability-mismatch"
    assert "NOT NULL" in found["message"]


def test_an_arity_mismatch_is_reported(project: CheckProject) -> None:
    (found,) = reports(project, 'query[tuple[int]](t"SELECT id, name FROM users")\n')
    assert found["rule"] == "arity-mismatch"


def test_a_column_name_mismatch_is_reported(project: CheckProject) -> None:
    source = (
        "from dataclasses import dataclass\n\n\n@dataclass\nclass User:\n    id: int\n    nickname: str\n\n\n"
        'query[User](t"SELECT id, name FROM users")\n'
    )
    found = reports(project, source)
    assert sorted(item["rule"] for item in found) == ["missing-column", "unexpected-column"]


def test_an_untyped_parameter_is_a_note_not_an_error(project: CheckProject) -> None:
    write(project, 'x = 1\nquery[tuple[int]](t"SELECT id FROM users WHERE name = {x}")\n')
    code, out, err = project.check("--inspect")
    assert (code, err) == (0, "")
    assert 'parameter ?1: SQL "", Python "int", untyped, not compared' in out


def test_an_untyped_column_is_a_note_not_an_error(project: CheckProject) -> None:
    write(project, 'query[tuple[int | None, str | None]](t"SELECT a, b FROM loose")\n')
    code, out, err = project.check("--inspect")
    assert (code, err) == (0, "")
    assert 'column 1: "a", SQL "", Python untyped, nullable' in out
    assert "error" not in out


def test_an_untyped_column_still_has_its_nullability_checked(project: CheckProject) -> None:
    (found,) = reports(project, 'query[tuple[int, str | None]](t"SELECT a, b FROM loose")\n')
    assert found["rule"] == "nullability-mismatch"
    assert 'column 1 "a" is untyped, so its type is not checked' in found["notes"]


def test_a_sql_error_carries_its_code_and_its_source_span(project: CheckProject) -> None:
    source = 'query[tuple[int, str]](t"SELECT id, nam FROM users")\n'
    (found,) = reports(project, source)
    line, col = position(source, "nam")
    assert (found["rule"], found["sqlstate"], found["code"]) == ("sql-error", "HY000", "SQLITE_ERROR")
    assert (found["line"], found["col"]) == (line, col)
    assert found["message"] == "no such column: nam (SQLITE_ERROR)"
    assert found["sql"] == "SELECT id, nam FROM users"


def test_a_sql_error_prints_its_code(project: CheckProject) -> None:
    project.write(HEADER + 'query[tuple[int]](t"SELECT FROM users")\n')
    code, out, err = project.check()
    assert (code, err) == (1, "")
    assert "error[sql-error]" in out
    assert "(SQLITE_ERROR)" in out


def test_query_unchecked_is_not_checked(project: CheckProject) -> None:
    source = 'query_unchecked[tuple[int, int]](t"SELECT nam, name FROM nowhere")\n'
    assert reports(project, source) == []


def test_user_code_is_never_imported(project: CheckProject) -> None:
    marker = project.root / "imported.marker"
    (project.root / "boom.py").write_text(
        f'from pathlib import Path\n\nPath({str(marker)!r}).write_text("x")\nraise RuntimeError("imported")\n',
        encoding="utf-8",
    )
    (project.root / "models.py").write_text(
        "from dataclasses import dataclass\n\nimport boom\n\n\n@dataclass\nclass User:\n    id: int\n    name: str\n",
        encoding="utf-8",
    )
    source = 'from models import User\n\nquery[User](t"SELECT id, name FROM users")\n'
    assert reports(project, "import boom\n" + source) == []
    assert not marker.exists()


def test_a_memory_url_implies_scratch_and_checks_the_migrated_schema(
    project: CheckProject, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the scratch file is made in the temporary directory, so the run's own is the only one there
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch_dir))
    memory = "sqlite::memory:"
    assert reports(project, 'query[tuple[int, str]](t"SELECT id, name FROM users")\n', url=memory) == []
    (found,) = reports(project, 'query[tuple[str, str]](t"SELECT id, name FROM users")\n', url=memory)
    assert found["rule"] == "type-mismatch"
    (error,) = reports(project, 'query[tuple[int]](t"SELECT nam FROM users")\n', url=memory)
    assert error["code"] == "SQLITE_ERROR"
    assert list(scratch_dir.iterdir()) == []
    # nothing was written to the project's own database file by a run against memory
    assert not (project.root / "sqlite::memory:").exists()


def test_a_memory_url_needs_the_migrate_table(project: CheckProject) -> None:
    (project.root / "pyproject.toml").write_text("", encoding="utf-8")
    project.write(HEADER + 'query[tuple[int]](t"SELECT 1")\n')
    code, out, err = project.check(url="sqlite::memory:")
    assert code != 0
    assert "[tool.rowx.migrate]" in out + err


def test_diagnostics_map_through_nested_expressions_and_padded_placeholders(project: CheckProject) -> None:
    # `={..}AND` and `>{x}OR` are padded with a space in the SQL, the `{{` is one `{`, and the holes hold braces and
    # quotes of their own
    source = (
        "x = 1\n"
        'query[tuple[int]](t"""\n'
        "    SELECT id\n"
        "    FROM users\n"
        "    WHERE id ={ {1: 2}[1] }AND age >{x}OR name = '{{' AND bogus = {'a{b' + 'c'}\n"
        '""")\n'
    )
    (found,) = reports(project, source)
    line, col = position(source, "bogus")
    assert (found["rule"], found["code"]) == ("sql-error", "SQLITE_ERROR")
    assert (found["line"], found["col"]) == (line, col)
    assert "bogus" in found["message"]
    assert found["sql"] is not None
    assert "?1AND" not in found["sql"]
    assert "?1 AND" in found["sql"]


def test_an_element_diagnostic_lands_on_its_annotation_after_padded_placeholders(project: CheckProject) -> None:
    source = 'x = 1\nquery[tuple[int, str]](t"SELECT id,{x}||email FROM users WHERE id ={x}OR name={x}")\n'
    (found,) = reports(project, source)
    assert found["rule"] == "nullability-mismatch"
    assert (found["line"], found["col"]) == position(source, "str]")


@pytest.mark.parametrize(
    "source",
    [
        'query[tuple[int]](t"SELECT id FROM users WHERE id = $1")\n',
        'query[tuple[int]](t"SELECT id FROM users WHERE id = ?")\n',
        'query[tuple[int]](t"SELECT id FROM users WHERE id = ?1")\n',
        'query[tuple[int]](t"SELECT id FROM users WHERE id = :name")\n',
        'query[tuple[int]](t"SELECT id FROM users WHERE id = @name")\n',
        'query[tuple[int]](t"SELECT id FROM users WHERE id = #name")\n',
    ],
)
def test_a_static_parameter_is_a_located_diagnostic(project: CheckProject, source: str) -> None:
    (found,) = reports(project, source)
    line, _ = position(source, "query[")
    assert found["rule"] == "sql-error"
    assert found["line"] == line
    assert found["col"] == position(source, 't"')[1]
    assert "in static SQL is a parameter in SQLite" in found["message"]
    assert "use an interpolation" in found["message"]


def test_an_interpolation_inside_a_string_is_a_located_diagnostic(project: CheckProject) -> None:
    source = "x = 'a'\nquery[tuple[int]](t\"SELECT id FROM users WHERE name = '{x}'\")\n"
    (found,) = reports(project, source)
    assert found["rule"] == "sql-error"
    assert "an interpolation must not be inside a string literal" in found["message"]


def test_other_queries_in_the_file_are_still_checked_after_a_static_parameter(project: CheckProject) -> None:
    source = 'query[tuple[int]](t"SELECT $1")\nquery[tuple[int]](t"SELECT nam FROM users")\n'
    found = reports(project, source)
    assert [item["line"] for item in found] == [3, 4]
    assert [item.get("code") for item in found] == [None, "SQLITE_ERROR"]


SQL_ERROR_SOURCE = (
    'query[tuple[int]](t"SELECT id FROM users; SELECT 1")\n'
    'query[tuple[int]](t"")\n'
    'query[tuple[int]](t"-- nothing")\n'
    'query[tuple[int]](t"SELECT nam FROM users")\n'
)


def test_sql_that_is_not_one_statement_is_reported_and_the_run_goes_on(project: CheckProject) -> None:
    write(project, SQL_ERROR_SOURCE)
    code, out, err = project.check("--no-drift-check", "--output-format", "json")
    assert "lost the database connection" not in out + err
    assert code == 1
    assert not err
    found = json.loads(out)
    assert [item["rule"] for item in found] == ["sql-error"] * 4
    assert [item["line"] for item in found] == [3, 4, 5, 6]
    assert found[0]["sqlstate"] == "HY000"
    assert "code" not in found[0]
    assert found[3]["code"] == "SQLITE_ERROR"


def test_sql_that_is_not_one_statement_is_reported_in_text_output(project: CheckProject) -> None:
    write(project, SQL_ERROR_SOURCE)
    code, out, err = project.check("--no-drift-check")
    assert code == 1
    assert "lost the database connection" not in out + err
    assert out.count("error[sql-error]") == 4


def test_a_nested_template_in_query_unchecked_is_not_checked(project: CheckProject) -> None:
    source = 'part = t"id"\nquery_unchecked[tuple[int]](t"SELECT {part} FROM nowhere")\n'
    assert reports(project, source) == []


def test_scratch_with_a_file_url_checks_the_migrated_scratch_and_leaves_no_file(
    project: CheckProject, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch_dir))
    # an empty file has no `users` table, so a pass can only come from the migrated scratch
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")
    url = f"sqlite:///{empty}"
    assert reports(project, 'query[tuple[int, str]](t"SELECT id, name FROM users")\n', "--scratch", url=url) == []
    (error,) = reports(project, 'query[tuple[int]](t"SELECT nam FROM users")\n', "--scratch", url=url)
    assert error["code"] == "SQLITE_ERROR"
    assert list(scratch_dir.glob("rowx_check_*")) == []
    assert empty.read_bytes() == b""
