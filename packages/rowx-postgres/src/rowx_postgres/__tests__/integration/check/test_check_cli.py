import asyncio
import json
import os
import re
import secrets
import shutil
import signal
import subprocess  # ruff: ignore[suspicious-subprocess-import]
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote, urlparse, urlunparse

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from rowx_core.errors import DatabaseError
from rowx_postgres import Connection, Migrator, embedded
from rowx_postgres.__tests__.integration.check.harness import (
    CASES_DIR,
    Expectation,
    check_case,
    diff,
    parse_markers,
    require_companion,
)
from rowx_postgres.__tests__.integration.check.test_render import (
    GENERIC_MODELS,
    GENERIC_QUERIES,
    MODELS,
    PYDANTIC_MODELS,
    PYDANTIC_QUERIES,
    QUERIES,
)
from rowx_postgres._core import connect_checker
from rowx_postgres._services import PostgresMigrationSession

from rowx import query_unchecked
from rowx.check.cli import main
from rowx.check.drift import read_history
from rowx.check.runner import CheckError, describe_all as describe_all_impl

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from rowx.check.runner import Outcome

pytestmark = [pytest.mark.db]

GOLDEN_DIR: Final[Path] = Path(__file__).resolve().parent / "golden"
OK_BASIC: Final[Path] = CASES_DIR / "ok_basic.py"
NULLABILITY_ERROR: Final[str] = """\
from rowx import query


def run() -> None:
    query[tuple[int, str, str]](t"SELECT id, name, email FROM users")
"""


def _dsn() -> str:
    # an empty DSN and a missing DSN fail differently
    if (dsn := os.environ.get("ROWX_TEST_DSN")) is not None and dsn != "":  # ruff: ignore[compare-to-empty-string]
        return dsn
    if os.environ.get("ROWX_REQUIRE_DB") == "1":
        pytest.fail("ROWX_REQUIRE_DB=1 but ROWX_TEST_DSN is not set")
    pytest.skip("ROWX_TEST_DSN is not set")


def _write(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return path


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(["check", *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_ok_basic_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = _run(capsys, str(OK_BASIC), "--database-url", _dsn())
    assert (code, out, err) == (0, "", "")


def test_ok_basic_matches_its_markers() -> None:
    assert check_case(OK_BASIC, _dsn()) == []


def test_null_parameter_preserves_default_output(
    schema: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write(tmp_path, "null.py", "from rowx import query\nquery[int | None](t'SELECT {None}::int')\n")
    assert _run(capsys, str(path), "--database-url", schema) == (0, "", "")
    assert _run(capsys, str(path), "--database-url", schema, "--output-format", "json") == (0, "[]\n", "")
    path.write_text("from rowx import query\nquery[int](t'SELECT 1 WHERE {None}::xml IS NULL')\n", encoding="utf-8")
    code, out, err = _run(capsys, str(path), "--database-url", schema)
    assert code == 1
    assert "error[unsupported-type]" in out
    assert not err
    code, out, err = _run(capsys, str(path), "--database-url", schema, "--output-format", "json")
    assert code == 1
    assert [item["rule"] for item in json.loads(out)] == ["unsupported-type"]
    assert not err


def test_postgres_reexports_match_checker_types(
    schema: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _write(
        tmp_path,
        "postgres_types.py",
        """\
from rowx import query
from rowx.postgres import Point, Range

query[Point](t'SELECT {Point(1.0, 2.0)}::point AS "point!"')
query[Range[int]](t'SELECT {Range(1, 5)}::int4range AS "range!"')
query[int](t'SELECT {Point(1.0, 2.0)}::int AS "number!"')
""",
    )
    code, out, err = _run(capsys, str(path), "--database-url", schema)
    assert code == 1
    assert out.count("error[") == 1
    assert "error[param-type-mismatch]" in out
    assert not err


def test_unreachable_database_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    url = "postgresql://nobody:hunter2secret@127.0.0.1:1/nope?connect_timeout=2"
    code, out, err = _run(capsys, str(OK_BASIC), "--database-url", url)
    assert code == 2
    assert not out
    assert "could not connect" in err


def test_missing_database_url_exits_two(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    code, _, err = _run(capsys, str(OK_BASIC))
    assert code == 2
    assert "no database URL" in err


def test_unknown_config_key_exits_two(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = _write(tmp_path, "pyproject.toml", '[tool.rowx]\ninclud = ["src"]\n')
    code, _, err = _run(capsys, str(OK_BASIC), "--config", str(config), "--database-url", _dsn())
    assert code == 2
    assert "unknown key 'includ'" in err
    assert "did you mean 'include'" in err


def test_no_queries_exits_zero_without_a_database(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    path = _write(tmp_path, "empty.py", "x = 1\n")
    assert _run(capsys, str(path)) == (0, "", "")


def test_nullability_error_exits_one(schema: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _write(tmp_path, "users.py", NULLABILITY_ERROR)
    code, out, _ = _run(capsys, str(path), "--database-url", schema)
    assert code == 1
    assert "error[nullability-mismatch]" in out
    assert 'column 3 "email" may be NULL' in out
    assert "^^^ element 3" in out


@pytest.mark.server(needs="sessions")
def test_jobs_produce_identical_output(schema: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    line = '    query[tuple[int, str, str]](t"SELECT id, name, email FROM users WHERE id > __N__")\n'
    source = "from rowx import query\n\n\ndef run() -> None:\n" + "".join(
        line.replace("__N__", str(n)) for n in range(12)
    )
    path = _write(tmp_path, "many.py", source)
    one = _run(capsys, str(path), "--database-url", schema, "-j", "1", "--statistics")
    four = _run(capsys, str(path), "--database-url", schema, "-j", "4", "--statistics")
    assert one == four
    assert one[0] == 1
    assert one[1].count("error[nullability-mismatch]") == 12


def test_statistics(schema: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = (
        "from rowx import query, query_unchecked\n\n\ndef run() -> None:\n"
        '    query[tuple[int, str, str]](t"SELECT id, name, email FROM users")\n'
        '    query[tuple[int, str, str]](t"SELECT id, name, email FROM users")\n'
        '    query[int](t"SELECT id FROM users")\n'
        '    query_unchecked(t"SELECT 1")\n'
    )
    path = _write(tmp_path, "stats.py", source)
    code, out, _ = _run(capsys, str(path), "--database-url", schema, "--statistics")
    assert code == 1
    assert out.endswith(
        "files scanned: 1\n"
        "call sites found: 4\n"
        "call sites checked: 3\n"
        "distinct SQL strings described: 2\n"
        "query_unchecked calls skipped: 1\n"
        "diagnostics: 2\n"
        "  nullability-mismatch: 2\n",
    )


def test_json_output(schema: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    clean = _run(capsys, str(OK_BASIC), "--database-url", _dsn(), "--output-format", "json", "--statistics")
    assert clean[0] == 0
    assert json.loads(clean[1]) == []
    assert "files scanned: 1" in clean[2]

    path = _write(tmp_path, "users.py", NULLABILITY_ERROR)
    code, out, err = _run(capsys, str(path), "--database-url", schema, "--output-format", "json")
    assert (code, err) == (1, "")
    (item,) = json.loads(out)
    assert item["rule"] == "nullability-mismatch"
    assert item["file"].endswith("users.py")
    assert item["column_index"] == 2
    assert item["line"] == 5


def test_sql_error_points_into_the_template(schema: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = 'from rowx import query\n\n\ndef run() -> None:\n    query[int](t"SELECT id FROM usrs")\n'
    path = _write(tmp_path, "typo.py", source)
    code, out, _ = _run(capsys, str(path), "--database-url", schema)
    assert code == 1
    assert "error[sql-error]" in out
    assert ":5:33" in out


def test_password_never_appears(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    secret = "hunter2secret"  # ruff: ignore[hardcoded-password-string]
    path = _write(tmp_path, "q.py", 'from rowx import query\n\nquery[int](t"SELECT 1")\n')
    urls = [
        f"postgresql://someone:{secret}@127.0.0.1:1/db?connect_timeout=2",
        f"host=127.0.0.1 port=1 user=someone password={secret} dbname=db connect_timeout=2",
    ]
    for url in urls:
        for fmt in ("text", "json"):
            code, out, err = _run(capsys, str(path), "--database-url", url, "--output-format", fmt, "--statistics")
            assert code == 2
            assert secret not in out + err
            assert url not in out + err


def test_password_never_appears_in_any_dsn_form(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _write(tmp_path, "q.py", 'from rowx import query\n\nquery[int](t"SELECT 1")\n')
    urls = [
        "postgresql://someone:p%40ss@127.0.0.1:1/db?connect_timeout=2",
        "postgresql://someone@127.0.0.1:1/db?password=p%40ss&connect_timeout=2",
        "host=127.0.0.1 port=1 user=someone password='p@ss' dbname=db connect_timeout=2",
        "postgresql://someone:p%40ss@127.0.0.1:notaport/db",
        "host=127.0.0.1 password=p@ss oops",
    ]
    for url in urls:
        code, out, err = _run(capsys, str(path), "--database-url", url, "--output-format", "text")
        assert code == 2
        assert "p@ss" not in out + err
        assert "p%40ss" not in out + err


def test_harness_rejects_a_wrong_marker(tmp_path: Path) -> None:
    source = (
        "from rowx import query\n\n"
        'query[int](t"SELECT 1 AS \\"n!\\"")  # error: [type-mismatch]\n'
        'query[tuple[int, int]](t"SELECT 1 AS \\"n!\\"")\n'
    )
    path = _write(tmp_path, "wrong.py", source)
    problems = check_case(path, _dsn())
    assert problems == [
        "line 3: expected [type-mismatch] but it was not reported",
        "line 4: unexpected [arity-mismatch]",
    ]


def test_harness_accepts_a_right_marker(tmp_path: Path) -> None:
    source = 'from rowx import query\n\nquery[tuple[int, int]](t"SELECT 1 AS \\"n!\\"")  # error: [arity-mismatch]\n'
    assert check_case(_write(tmp_path, "right.py", source), _dsn()) == []


def test_parse_markers() -> None:
    source = "a\nb  # error: [x, y]\nc  # error:[z]\n"
    expected: list[Expectation] = parse_markers(source)
    assert [(e.line, e.rule) for e in expected] == [(2, "x"), (2, "y"), (3, "z")]


LIVE_RULES: Final[str] = """\
from rowx import query

q = "x"


def run() -> None:
    query[int](t"SELECT id FROM users WHERE name LIKE '%{q}%'")  # error: [param-count-mismatch]
    query[int](t"SELECT id FROM users WHERE name = {q}")
    query(t"SELECT id FROM users")  # error: [missing-row-type]
    query[None](t"SELECT pg_sleep(0)")
    query[int](t"SELECT pg_sleep(0)")  # error: [unexpected-row-type]
    query[int](t"UPDATE users SET name = name")  # error: [unexpected-row-type]
    query[tuple[int, str]](t"SELECT id FROM users")  # error: [arity-mismatch]
    query[list[int]](t'SELECT ARRAY[1, 2] AS "a!"')
    query[list[str]](t'SELECT ARRAY[1, 2] AS "a!"')  # error: [type-mismatch]
    query[str](t'SELECT current_user::text AS "__LONG__!"')  # error: [invalid-override, nullability-mismatch]
    query[int](t"SELEC 1")  # error: [sql-error]
"""


def test_live_rules_match_markers(schema: str, tmp_path: Path) -> None:
    path = _write(tmp_path, "live.py", LIVE_RULES.replace("__LONG__", "a" * 70))
    assert check_case(path, schema) == []


def test_comment_text_is_not_an_alias(
    schema: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    name = "a" * 63
    source = f'from rowx import query\n\nquery[str](t\'SELECT current_user::text AS "{name}" /* "{name}extra!" */ \')\n'
    ok = _run(capsys, str(_write(tmp_path, "alias.py", source)), "--database-url", schema)
    assert "invalid-override" not in ok[1]


def test_source_encoding_is_honoured(schema: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    text = '# -*- coding: latin-1 -*-\nfrom rowx import query\n\nx = "é"; query[int](t"SELECT id FROM usrs")\n'
    path = tmp_path / "latin.py"
    path.write_bytes(text.encode("latin-1"))
    code, out, _ = _run(capsys, str(path), "--database-url", schema)
    assert code == 1
    assert "é" in out
    assert "\ufffd" not in out
    assert ":4:" in out


DRIFT_QUERY: Final[str] = 'from rowx import query\n\nquery[int](t"SELECT id FROM users")\n'


@pytest.fixture
def drift_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "pyproject.toml", "[tool.rowx.migrate]\n")
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    _write(migrations, "1_create_drift_a.sql", "CREATE TABLE drift_a (id int4);")
    _write(migrations, "2_create_drift_b.sql", "CREATE TABLE drift_b (id int4);")
    _write(tmp_path, "q.py", DRIFT_QUERY)
    return tmp_path


def _migrate(capsys: pytest.CaptureFixture[str], dsn: str, *argv: str) -> int:
    code = main(["migrate", "run", *argv, "--database-url", dsn])
    capsys.readouterr()
    return code


def _drift(capsys: pytest.CaptureFixture[str], dsn: str, *argv: str) -> tuple[int, str, str]:
    return _run(capsys, "q.py", "--database-url", dsn, *argv)


def _golden(name: str) -> str:
    return (GOLDEN_DIR / name).read_text(encoding="utf-8")


@pytest.mark.usefixtures("drift_project")
def test_drift_up_to_date_passes(schema: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert _migrate(capsys, schema) == 0
    assert _drift(capsys, schema) == (0, "", "")
    code, out, err = _drift(capsys, schema, "--statistics")
    assert (code, err) == (0, "")
    assert "migrations checked: 2\n" in out


@pytest.mark.usefixtures("drift_project")
def test_drift_pending_exits_two(schema: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert _drift(capsys, schema) == (2, "", _golden("drift_pending.txt"))
    # the gate creates nothing, and the missing history table counts as every migration pending
    with psycopg.connect(schema, autocommit=True) as conn:
        row = conn.execute("SELECT to_regclass('_rowx_migrations') IS NULL").fetchone()
    assert row == (True,)


@pytest.mark.usefixtures("drift_project")
def test_drift_partially_pending(schema: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert _migrate(capsys, schema, "--target-version", "1") == 0
    code, out, err = _drift(capsys, schema)
    assert (code, out) == (2, "")
    assert "  1 pending: 2\n" in err


def test_drift_modified_exits_two(schema: str, drift_project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _migrate(capsys, schema) == 0
    _write(drift_project / "migrations", "2_create_drift_b.sql", "CREATE TABLE drift_b (id int8);")
    assert _drift(capsys, schema) == (2, "", _golden("drift_modified.txt"))


def test_drift_missing_exits_two(schema: str, drift_project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _migrate(capsys, schema) == 0
    (drift_project / "migrations" / "2_create_drift_b.sql").unlink()
    assert _drift(capsys, schema) == (2, "", _golden("drift_missing.txt"))


def test_drift_dirty_exits_two(schema: str, drift_project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _migrate(capsys, schema) == 0
    _write(
        drift_project / "migrations", "3_dirty.sql", "-- no-transaction\nCREATE TABLE drift_c (id int4);\nSELECT 1/0;"
    )
    assert _migrate(capsys, schema) == 2
    assert _drift(capsys, schema) == (2, "", _golden("drift_dirty.txt"))


@pytest.mark.usefixtures("drift_project")
def test_drift_json_keeps_stdout_empty(schema: str, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = _drift(capsys, schema, "--output-format", "json")
    assert (code, out) == (2, "")
    assert err == _golden("drift_pending.txt")


@pytest.mark.usefixtures("drift_project")
def test_no_drift_check_bypasses_the_gate(schema: str, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = _drift(capsys, schema, "--no-drift-check", "--statistics")
    assert (code, out.count("migrations checked"), err) == (0, 0, "")


def test_without_migrate_config_there_is_no_gate(
    schema: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "pyproject.toml", "")
    _write(tmp_path, "q.py", NULLABILITY_ERROR)
    code, out, err = _run(capsys, "q.py", "--database-url", schema, "--statistics")
    assert (code, err) == (1, "")
    assert "migrations checked" not in out


def test_drift_gate_skipped_when_nothing_is_described(
    drift_project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # with no query to describe there is no checker session, so neither a URL nor an up-to-date database is needed
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _write(drift_project, "q.py", "x = 1\n")
    code, out, err = _run(capsys, "q.py", "--statistics")
    assert (code, err) == (0, "")
    assert "migrations checked" not in out


@pytest.mark.usefixtures("drift_project")
def test_drift_password_never_appears(capsys: pytest.CaptureFixture[str]) -> None:
    url = "postgresql://someone:hunter2secret@127.0.0.1:1/db?connect_timeout=2"
    code, out, err = _drift(capsys, url)
    assert code == 2
    assert "hunter2secret" not in out + err


def test_history_is_read_in_a_read_only_session(schema: str, drift_project: Path) -> None:
    async def run() -> None:
        migrator = Migrator(drift_project / "migrations")
        conn = Connection(await connect_checker(schema, 10.0))
        try:
            statuses = await read_history(PostgresMigrationSession(migrator, conn))
            assert [s.state for s in statuses] == ["pending", "pending"]
            # the same session refuses a write
            with pytest.raises(DatabaseError) as raised:
                await query_unchecked[None](t"CREATE TABLE drift_nope (id int4)").execute(conn)
            assert raised.value.sqlstate == "25006"
        finally:
            await conn.close()

    asyncio.run(run())


SCRATCH_MIGRATION: Final[str] = "CREATE TABLE scratch_users (id int4 PRIMARY KEY, name text NOT NULL, email text);"
SCRATCH_CLEAN_QUERY: Final[str] = (
    'from rowx import query\n\nquery[tuple[int, str]](t"SELECT id, name FROM scratch_users")\n'
)
SCRATCH_DIAGNOSTIC_QUERY: Final[str] = (
    'from rowx import query\n\nquery[tuple[int, str, str]](t"SELECT id, name, email FROM scratch_users")\n'
)


@pytest.fixture
def scratch_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "pyproject.toml", "[tool.rowx.migrate]\n")
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    _write(migrations, "1_create_scratch_users.sql", SCRATCH_MIGRATION)
    _write(tmp_path, "q.py", SCRATCH_CLEAN_QUERY)
    return tmp_path


def _scratch_databases(dsn: str) -> set[str]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        rows = conn.execute("SELECT datname FROM pg_database WHERE datname LIKE 'rowx\\_check\\_%'").fetchall()
    return {row[0] for row in rows}


@pytest.fixture
def no_leftovers() -> Iterator[str]:
    dsn = _dsn()
    before = _scratch_databases(dsn)
    yield dsn
    leaked = _scratch_databases(dsn) - before
    assert not leaked, f"scratch databases were not dropped: {sorted(leaked)}"


def _scratch(capsys: pytest.CaptureFixture[str], dsn: str, *argv: str) -> tuple[int, str, str]:
    return _run(capsys, "q.py", "--database-url", dsn, "--scratch", *argv)


def test_scratch_without_migrate_config_is_a_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "pyproject.toml", "")
    _write(tmp_path, "q.py", SCRATCH_CLEAN_QUERY)
    code, out, err = _scratch(capsys, "postgresql://u@127.0.0.1:1/db")
    assert (code, out) == (2, "")
    assert "[tool.rowx.migrate]" in err


@pytest.mark.usefixtures("scratch_project")
@pytest.mark.server(needs="create-database")
def test_scratch_clean_run_when_the_target_lacks_the_tables(
    no_leftovers: str, capsys: pytest.CaptureFixture[str]
) -> None:
    # the drift gate would reject this database, which has no history table
    assert _drift(capsys, no_leftovers)[0] == 2
    assert _scratch(capsys, no_leftovers) == (0, "", "")


@pytest.mark.usefixtures("scratch_project")
@pytest.mark.server(needs="create-database")
def test_scratch_reports_diagnostics_against_the_scratch_schema(
    no_leftovers: str, scratch_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(scratch_project, "q.py", SCRATCH_DIAGNOSTIC_QUERY)
    code, out, err = _scratch(capsys, no_leftovers)
    assert (code, err) == (1, "")
    assert "q.py:3" in out
    assert "nullable" in out


@pytest.mark.usefixtures("scratch_project")
@pytest.mark.server(needs="create-database")
def test_scratch_never_touches_the_target_database(no_leftovers: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert _scratch(capsys, no_leftovers)[0] == 0
    with psycopg.connect(no_leftovers, autocommit=True) as conn:
        row = conn.execute("SELECT to_regclass('scratch_users'), to_regclass('_rowx_migrations')").fetchone()
    assert row == (None, None)


@pytest.mark.server(needs="create-database")
def test_scratch_failing_migration_exits_two(
    no_leftovers: str, scratch_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(scratch_project / "migrations", "2_explode.sql", "SELECT 1/0;")
    code, out, err = _scratch(capsys, no_leftovers)
    assert (code, out) == (2, "")
    assert "2" in err
    assert "division by zero" in err


@pytest.mark.server(needs="create-database")
def test_scratch_honours_the_migrate_table_and_create_schemas(
    no_leftovers: str, scratch_project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # the history table goes in a schema that only `create_schemas` creates, as `rowx migrate run` would
    _write(
        scratch_project,
        "pyproject.toml",
        '[tool.rowx.migrate]\ntable = "rowx_meta.history"\ncreate_schemas = ["rowx_meta", "rowx_app"]\n',
    )
    _write(scratch_project / "migrations", "1_create_scratch_users.sql", "CREATE TABLE rowx_app.users (id int4);")
    _write(
        scratch_project,
        "q.py",
        'from rowx import query\n\nquery[tuple[int | None]](t"SELECT id FROM rowx_app.users")\n',
    )
    tables: list[tuple[str | None, str | None]] = []
    original = describe_all_impl

    async def describe_all(urls: Sequence[str], *args: Any) -> dict[str, Outcome]:  # ruff: ignore[any-type]
        conn = Connection(await connect_checker(urls[0], 10.0))
        try:
            tables.append(
                await query_unchecked[tuple[str | None, str | None]](
                    t"SELECT to_regclass('rowx_meta.history')::text, to_regclass('public._rowx_migrations')::text"
                ).one(conn)
            )
        finally:
            await conn.close()
        return await original(urls, *args)

    monkeypatch.setattr("rowx.check.runner.describe_all", describe_all)
    assert _scratch(capsys, no_leftovers) == (0, "", "")
    assert tables == [("rowx_meta.history", None)]


@pytest.mark.usefixtures("scratch_project")
@pytest.mark.server(needs="create-database")
def test_scratch_never_migrates_a_database_named_in_the_query_string(
    no_leftovers: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    parts = urlparse(no_leftovers)
    if parts.scheme not in {"postgres", "postgresql"}:
        pytest.skip("ROWX_TEST_DSN is not a URL")
    with psycopg.connect(no_leftovers, autocommit=True) as conn:
        row = conn.execute("SELECT current_database()").fetchone()
    assert row is not None
    # the driver percent-decodes query keys, so `%64bname` selects a database just as `dbname` does
    url = urlunparse(parts._replace(query="&".join(filter(None, [parts.query, f"%64bname={quote(row[0])}"]))))
    described: list[str] = []
    original = describe_all_impl

    async def describe_all(urls: Sequence[str], *args: Any) -> dict[str, Outcome]:  # ruff: ignore[any-type]
        conn = Connection(await connect_checker(urls[0], 10.0))
        try:
            described.append(await query_unchecked[str](t"SELECT current_database()::text").one(conn))
        finally:
            await conn.close()
        return await original(urls, *args)

    monkeypatch.setattr("rowx.check.runner.describe_all", describe_all)
    assert _scratch(capsys, url) == (0, "", "")
    assert len(described) == 1
    assert described[0].startswith("rowx_check_")
    with psycopg.connect(no_leftovers, autocommit=True) as conn:
        tables = conn.execute("SELECT to_regclass('scratch_users'), to_regclass('_rowx_migrations')").fetchone()
    assert tables == (None, None)


@pytest.mark.usefixtures("scratch_project")
def test_scratch_without_createdb_exits_two_with_help(no_leftovers: str, capsys: pytest.CaptureFixture[str]) -> None:
    role = f"rowx_nocreatedb_{secrets.token_hex(6)}"
    password = secrets.token_hex(8)
    with psycopg.connect(no_leftovers, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN NOCREATEDB PASSWORD {}").format(sql.Identifier(role), sql.Literal(password))
        )
    try:
        url = make_conninfo(no_leftovers, user=role, password=password)
        code, out, err = _scratch(capsys, url)
    finally:
        with psycopg.connect(no_leftovers, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
    assert (code, out) == (2, "")
    assert "`--scratch` needs a role with CREATEDB" in err
    assert password not in err


@pytest.mark.usefixtures("scratch_project")
def test_scratch_unreachable_server_never_prints_the_password(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = _scratch(capsys, "postgresql://someone:hunter2secret@127.0.0.1:1/db?connect_timeout=2")
    assert code == 2
    assert "hunter2secret" not in out + err


@pytest.mark.usefixtures("scratch_project")
@pytest.mark.server(needs="create-database")
def test_scratch_statistics_report_the_build_time(no_leftovers: str, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = _scratch(capsys, no_leftovers, "--statistics")
    assert (code, err) == (0, "")
    assert "scratch database built in: " in out
    assert "migrations checked" not in out


@pytest.mark.usefixtures("scratch_project")
def test_scratch_is_not_built_when_nothing_is_described(
    scratch_project: Path, no_leftovers: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(scratch_project, "q.py", "x = 1\n")
    code, out, err = _scratch(capsys, no_leftovers, "--statistics")
    assert (code, err) == (0, "")
    assert "scratch database built" not in out


def _during_check(monkeypatch: pytest.MonkeyPatch, action: Callable[[], None]) -> list[str]:
    seen: list[str] = []

    async def describe_all(urls: Sequence[str], *_: object) -> dict[str, Outcome]:
        seen.append(urls[0])
        action()
        # reached only by an action that returns, such as a signal, which cancels the run
        await asyncio.Event().wait()
        return {}

    monkeypatch.setattr("rowx.check.runner.describe_all", describe_all)
    return seen


def _raise_interrupt() -> None:
    raise KeyboardInterrupt


def _raise_internal() -> None:
    msg = "boom"
    raise RuntimeError(msg)


def _raise_describe_failure() -> None:
    msg = "lost the database connection"
    raise CheckError(msg)


def _signal_interrupt() -> None:
    # the real path: asyncio.run turns SIGINT into a cancellation of the main task
    signal.raise_signal(signal.SIGINT)


@pytest.mark.usefixtures("scratch_project")
@pytest.mark.parametrize(
    ("action", "expected"),
    [
        (_raise_interrupt, "interrupted"),
        (_signal_interrupt, "interrupted"),
        (_raise_internal, "internal error"),
        (_raise_describe_failure, "lost the database connection"),
    ],
)
@pytest.mark.server(needs="create-database")
def test_scratch_is_dropped_when_the_check_is_interrupted_or_fails(
    no_leftovers: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    action: Callable[[], None],
    expected: str,
) -> None:
    seen = _during_check(monkeypatch, action)
    code, out, err = _scratch(capsys, no_leftovers)
    assert (code, out) == (2, "")
    assert expected in err
    # the check ran against a scratch database, which is gone once the fixture has checked pg_database
    assert len(seen) == 1
    assert "/rowx_check_" in seen[0]


@pytest.mark.usefixtures("scratch_project")
@pytest.mark.server(needs="create-database")
def test_scratch_check_session_is_read_only(
    no_leftovers: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    states: list[str] = []
    original = describe_all_impl

    async def describe_all(urls: Sequence[str], *args: Any) -> dict[str, Outcome]:  # ruff: ignore[any-type]
        conn = Connection(await connect_checker(urls[0], 10.0))
        try:
            states.append(await query_unchecked[str](t"SHOW default_transaction_read_only").one(conn))
        finally:
            await conn.close()
        return await original(urls, *args)

    monkeypatch.setattr("rowx.check.runner.describe_all", describe_all)
    assert _scratch(capsys, no_leftovers)[0] == 0
    assert states == ["on"]


def test_coverage_descriptions_and_parameter_evidence(
    schema: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _write(
        tmp_path,
        "coverage.py",
        """\
from rowx import query, query_unchecked

def run(unknown, known: int):
    query[int](t'SELECT {unknown}::int AS "value!"')
    query[int](t'SELECT {known}::int AS "value!"')
    query[int](t'SELECT {known}::int AS "value!"')
    query[int](t'SELECT {known}::text AS "value!"')
    query[int](t'SELECT nope FROM missing_table')
    query_unchecked(t'SELECT 1')
""",
    )
    code, out, err = _run(capsys, str(path), "--database-url", schema, "--coverage", "--statistics")
    assert code == 1
    assert not err
    assert "recognized query sites: 6" in out
    assert out.count(": described") == 4
    assert "coverage.py:5:5-5:51: described" in out
    assert "coverage.py:6:5-6:51: described" in out
    assert ": not described" in out
    assert ": unchecked" in out
    assert "Python unknown; not compared" in out
    assert "Python int; no mismatch reported" in out
    assert "Python int; mismatch" in out
    assert "distinct SQL strings described: 3" in out
    assert "error[sql-error]" in out


def test_documented_order_branches_are_both_checked(
    schema: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _write(
        tmp_path,
        "order.py",
        """\
from rowx import query

def ordered(descending: bool = False):
    if descending:
        return query[int](t'SELECT 1 AS "value!" ORDER BY 1 DESC')
    return query[int](t'SELECT 1 AS "value!" ORDER BY 1 ASC')
""",
    )
    code, out, _ = _run(capsys, str(path), "--database-url", schema, "--require-checked", "--coverage")
    assert code == 0
    assert out.count(": described") == 2
    path.write_text(path.read_text().replace("ORDER BY 1 DESC", "ORDER BY missing DESC"), encoding="utf-8")
    code, out, _ = _run(capsys, str(path), "--database-url", schema, "--require-checked", "--coverage")
    assert code == 1
    assert "error[sql-error]" in out
    assert "order.py:5:" in out
    assert "not described" in out
    assert "order.py:6:" in out
    assert ": described" in out


def test_coverage_array_evidence_and_unresolved_rows(
    schema: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _write(
        tmp_path,
        "array_coverage.py",
        """\
from rowx import query

def run(items: list, unknown):
    query[list[int]](t'SELECT {[1, 2]}::int[] AS "value!"')
    query[list[int]](t'SELECT {items}::int[] AS "value!"')
    query[Missing](t'SELECT {unknown}::int AS "value!"')
""",
    )
    code, out, _ = _run(capsys, str(path), "--database-url", schema, "--coverage")
    assert code == 1
    assert "Python list; no mismatch reported" in out
    assert "SQL _int4, Python unknown; not compared" in out
    assert "SQL int4, Python unknown; not compared" in out
    assert out.count("row contract: compared") == 2
    assert out.count("row contract: not compared") == 1


@pytest.mark.server(divergence="xml")
def test_coverage_partial_row_comparisons(
    schema: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _write(
        tmp_path,
        "row_coverage.py",
        """\
from pydantic import AliasChoices, BaseModel, Field
from rowx import query

class Row(BaseModel):
    id: int = Field(validation_alias=AliasChoices("a", "b"))
    name: int

def run():
    query[int](t'SELECT 1 AS "value!"')
    query[Missing](t'SELECT 1 AS "value!"')
    query[tuple[int, int]](t'SELECT 1 AS "value!"')
    query[tuple[int, str]](t'''SELECT 1 AS "value!", '<a/>'::xml AS "p!"''')
    query[Row](t'SELECT 1 AS "a!", 2 AS "name!"')
    query[int](t'SELECT NULL::text AS "value?"')
""",
    )
    code, out, _ = _run(capsys, str(path), "--database-url", schema, "--coverage")
    assert code == 1
    assert [line.strip() for line in out.splitlines() if "row contract:" in line] == [
        "row contract: compared",
        "row contract: not compared",
        "row contract: partially compared",
        "row contract: partially compared",
        "row contract: partially compared",
        "row contract: partially compared",
    ]
    assert "error[arity-mismatch]" in out
    assert "error[unsupported-type]" in out
    assert 'error[type-mismatch]: column 1 "value": column type is `str` but the annotation is `int`' in out
    assert "error[nullability-mismatch]" not in out


def test_inspect_contracts_and_deduplicated_sites(
    schema: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with psycopg.connect(schema, autocommit=True) as conn:
        conn.execute("CREATE TABLE inspect_a (id int NOT NULL, optional int)")
        conn.execute("CREATE TABLE inspect_b (id int NOT NULL)")
    path = _write(
        tmp_path,
        "inspect.py",
        """from rowx import query

def run(unknown, known: int):
    query[tuple[int, int | None, int | None, int, int | None]](t\'SELECT a.id, b.id, a.id + 1, a.id AS "forced!", a.id AS "optional?" FROM inspect_a a LEFT JOIN inspect_b b ON a.id = b.id\')
    query[int](t\'SELECT optional FROM inspect_a WHERE optional IS NOT NULL\')
    query[int](t\'SELECT {unknown}::int AS "value!"\')
    query[int](t\'SELECT {known}::int AS "value!"\')
    query[int](t\'SELECT nope FROM missing_table\')
""",
    )
    code, out, err = _run(capsys, str(path), "--database-url", schema, "--inspect", "--statistics")
    assert code == 1
    assert not err
    assert out.count('column 1: "value", SQL "int4", Python "int"') == 2
    assert "Python unknown, not compared" in out
    assert 'Python "int", no mismatch reported' in out
    assert "forced NOT NULL by the" in out
    assert "nullability unknown, assumed nullable" in out
    assert "nullable side of an outer join" in out
    assert "scan filter rejects NULL" in out
    assert "forced nullable" in out
    assert "declared NOT NULL" in out
    assert "distinct SQL strings described: 4" in out
    assert "error[sql-error]" in out
    assert 'inspect.py":6:' in out
    assert 'inspect.py":7:' in out
    code, out, err = _run(capsys, str(path), "--database-url", schema, "--inspect", "--output-format", "json")
    assert code == 1
    diagnostics = json.loads(out)
    diagnostic = next(item for item in diagnostics if item["rule"] == "sql-error")
    error_line = path.read_text().splitlines()[7]
    assert (diagnostic["line"], diagnostic["col"]) == (8, error_line.index("missing_table") + 1)
    code_plain, plain, plain_err = _run(capsys, str(path), "--database-url", schema, "--output-format", "json")
    assert code_plain == code
    assert plain == out
    assert not plain_err
    assert 'column 1: "value"' in err


def test_inspect_subprocess_never_evaluates_source(schema: str, tmp_path: Path) -> None:
    with psycopg.connect(schema, autocommit=True) as conn:
        conn.execute("CREATE TABLE inspect_sentinel (id int NOT NULL)")
    module_marker = tmp_path / "module-fired"
    annotation_marker = tmp_path / "annotation-fired"
    path = _write(
        tmp_path,
        "sentinels.py",
        f"""from pathlib import Path
from typing import Annotated
from rowx import query
Path({str(module_marker)!r}).touch()
def annotation():
    Path({str(annotation_marker)!r}).touch()
    return int
def run():
    query[Annotated[int, annotation()]](t'INSERT INTO inspect_sentinel VALUES (1) RETURNING id AS "value!"')
""",  # ruff: ignore[hardcoded-sql-expression]
    )
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [str(Path(sys.executable).with_name("rowx")), "check", str(path), "--database-url", schema, "--inspect"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert 'column 1: "value"' in result.stdout
    assert not module_marker.exists()
    assert not annotation_marker.exists()
    with psycopg.connect(schema) as conn:
        assert conn.execute("SELECT count(*) FROM inspect_sentinel").fetchone() == (0,)


@pytest.mark.server(needs="auth")
def test_inspect_sanitizes_identifiers_before_redaction(
    schema: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    password = conninfo_to_dict(schema).get("password")
    if not isinstance(password, str) or len(password) < 2:
        pytest.skip("the database URL needs a password for this test")
    hostile = password[0] + "\u200b" + password[1:] + "\n\tline"
    identifier = hostile.replace('"', '""')
    path = _write(tmp_path, "hostile.py", f"from rowx import query\nquery[int](t'''SELECT 1 AS \"{identifier}!\"''')\n")
    code, out, err = _run(capsys, str(path), "--database-url", schema, "--inspect")
    assert code == 0
    assert not err
    assert password not in out
    assert "\u200b" not in out
    assert "\\n\\tline" in out
    assert len(out.splitlines()) == 2


@pytest.mark.usefixtures("scratch_project")
@pytest.mark.server(needs="create-database")
def test_scratch_inspection_uses_migrated_schema(no_leftovers: str, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = _scratch(capsys, no_leftovers, "--inspect")
    assert code == 0
    assert not err
    assert 'column 1: "id", SQL "int4", Python "int"' in out
    assert 'column 2: "name", SQL "text", Python "str"' in out


def test_inspect_supported_arrays_and_unsupported_types(
    schema: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write(
        tmp_path,
        "types.py",
        "from rowx import query\n"
        "query[list[int]](t'SELECT ARRAY[1] AS \"values!\"')\n"
        "query[str | None](t'SELECT NULL::xml AS \"unknown\"')\n"
        "def arrays(good: list[list[int]], bad: list[str], unknown: list[Missing]) -> None:\n"
        "    query[list[int] | None](t'SELECT {good}::int4[]')\n"
        "    query[list[int] | None](t'SELECT {bad}::int4[]')\n"
        "    query[list[int] | None](t'SELECT {unknown}::int4[]')\n",
    )
    code, out, err = _run(capsys, str(path), "--database-url", schema, "--inspect")
    assert code == 1
    assert not err
    assert 'SQL "_int4", Python "list[int]"' in out
    assert 'SQL "xml", Python unknown' in out
    assert "error[unsupported-type]" in out
    assert 'SQL "_int4", Python "list[list[int]]", no mismatch reported' in out
    assert 'SQL "_int4", Python "list[str]", mismatch' in out
    assert 'SQL "_int4", Python unknown, not compared' in out


# --- embedded:// (these need the rowx-embedded companion, not a server) ---------------------------------------

CHECK_DIR: Final[Path] = Path(__file__).resolve().parent
EXPECTED_DIR: Final[Path] = CHECK_DIR / "expected"
SUPPORT_DIR: Final[Path] = CHECK_DIR.parents[1] / "support"
CASE_FILES: Final[list[Path]] = sorted(path for path in CASES_DIR.rglob("*.py") if path.name != "__init__.py")
ELAPSED: Final[re.Pattern[str]] = re.compile(r"\(\d+\.\d(?:us|ms|s)\)")
JSON_TIMES: Final[re.Pattern[str]] = re.compile(r'("installed_on": )"[^"]+"|("execution_time": )\d+')
RETRY_LIMIT: Final[float] = 10.0
EMBEDDED_DRIFT_QUERY: Final[str] = 'from rowx import query\n\nquery[tuple[int | None]](t"SELECT id FROM drift_a")\n'
EMBEDDED_CLEAN_QUERY: Final[str] = 'from rowx import query\n\nquery[tuple[int, str]](t"SELECT id, name FROM users")\n'


@pytest.fixture(scope="module")
def companion() -> None:
    require_companion()


# the embedded tests skip without the companion, and each one counts what its instances leave in a TMPDIR of its own
def embedded_only[F: Callable[..., object]](test: F) -> F:
    return pytest.mark.embedded(pytest.mark.usefixtures("companion", "private_tmp")(test))


# a short path of its own (a socket path is limited to about 100 bytes), so a leftover can be counted exactly
@pytest.fixture
def private_tmp(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    root = Path(tempfile.mkdtemp(prefix="rxc-"))
    monkeypatch.setenv("TMPDIR", str(root))
    yield root
    # the engine's own scratch files (`.tmp*`) are swept by a later start; a socket directory is never left
    leftovers = sorted(path.name for path in root.iterdir() if path.name.startswith("rxe-"))
    shutil.rmtree(root, ignore_errors=True)
    assert not leftovers, f"an instance left {leftovers} behind"


def _write_dirs(directory: Path, name: str, text: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def _schema_migration() -> str:
    return (SUPPORT_DIR / "schema.sql").read_text(encoding="utf-8") + (SUPPORT_DIR / "seed_data.sql").read_text(
        encoding="utf-8"
    )


@pytest.fixture(scope="module")
def embedded_schema_project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("embedded_schema")
    _write_dirs(root, "pyproject.toml", '[tool.rowx]\ninclude = ["."]\n\n[tool.rowx.migrate]\n')
    _write_dirs(root / "migrations", "1_schema.sql", _schema_migration())
    shutil.copytree(CASES_DIR, root / "cases")
    return root


def _run_cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _normalize(text: str) -> str:
    text = ELAPSED.sub("(<time>)", text)
    return JSON_TIMES.sub(lambda m: f'{m[1] or m[2]}"<time>"', text)


def _embedded_case_diagnostics(
    path: Path, project: Path, capsys: pytest.CaptureFixture[str], jobs: str
) -> tuple[int, list[Expectation], str]:
    target = project / "cases" / path.relative_to(CASES_DIR)
    code, out, err = _run_cli(
        capsys,
        "check",
        str(target),
        "--config",
        str(project / "pyproject.toml"),
        "--database-url",
        "embedded://",
        "-j",
        jobs,
        "--output-format",
        "json",
    )
    reports = json.loads(out)
    return code, sorted(Expectation(item["line"], item["rule"]) for item in reports), err


@embedded_only
@pytest.mark.parametrize("path", CASE_FILES, ids=lambda path: str(path.relative_to(CASES_DIR)))
def test_every_check_case_matches_its_markers_on_embedded(
    path: Path, embedded_schema_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, found, err = _embedded_case_diagnostics(path, embedded_schema_project, capsys, "1")
    assert not err
    assert diff(parse_markers(path.read_text(encoding="utf-8")), found) == []
    assert code == (1 if found else 0)


# a subset of the cases at -j 4 (every case at both job counts would double a module that boots an engine per run):
# one clean case, one per diagnostic family, a case with several statements, and a package with imports
FOUR_JOB_CASES: Final[tuple[str, ...]] = (
    "ok_basic.py",
    "ok_seeded.py",
    "nullability_mismatch.py",
    "sql_error.py",
    "param_type_mismatch.py",
    "arity_mismatch.py",
    "named_rows.py",
    "alias_pkg/cross_module_alias.py",
)


@embedded_only
@pytest.mark.parametrize("name", FOUR_JOB_CASES)
def test_representative_check_cases_match_their_markers_at_four_jobs(
    name: str, embedded_schema_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = CASES_DIR / name
    code, found, err = _embedded_case_diagnostics(path, embedded_schema_project, capsys, "4")
    assert not err
    assert diff(parse_markers(path.read_text(encoding="utf-8")), found) == []
    assert code == (1 if found else 0)


def _render_project(tmp_path: Path, models: str, queries: str) -> Path:
    _write_dirs(tmp_path / "app", "__init__.py", "")
    _write_dirs(tmp_path / "app", "models.py", models)
    _write_dirs(tmp_path, "queries.py", queries)
    _write_dirs(tmp_path, "pyproject.toml", '[tool.rowx]\ninclude = ["."]\n\n[tool.rowx.migrate]\n')
    _write_dirs(tmp_path / "migrations", "1_schema.sql", _schema_migration())
    return tmp_path


EMBEDDED_RENDER_SETS: Final[dict[str, tuple[str, str]]] = {
    "named_rows": (MODELS, QUERIES),
    "pydantic_rows": (PYDANTIC_MODELS, PYDANTIC_QUERIES),
    "generic_rows": (GENERIC_MODELS, GENERIC_QUERIES),
}


@embedded_only
@pytest.mark.parametrize("fmt", ["text", "json"])
@pytest.mark.parametrize("name", EMBEDDED_RENDER_SETS)
@pytest.mark.parametrize("jobs", ["1", "4"])
def test_rendered_reports_match_the_docker_goldens_at_any_job_count(
    name: str, fmt: str, jobs: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    models, queries = EMBEDDED_RENDER_SETS[name]
    root = _render_project(tmp_path, models, queries)
    code, out, err = _run_cli(
        capsys,
        "check",
        str(root / "queries.py"),
        "--config",
        str(root / "pyproject.toml"),
        "--database-url",
        "embedded://",
        "-j",
        jobs,
        *(["--output-format", "json"] if fmt == "json" else []),
    )
    assert (code, err) == (1, "")
    expected = EXPECTED_DIR / (f"{name}.json.txt" if fmt == "json" else f"{name}.txt")
    shown = out.replace(str(root.resolve()), "<root>").replace(str(root), "<root>")
    assert shown == expected.read_text(encoding="utf-8")


@pytest.fixture
def embedded_scratch_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    _write_dirs(tmp_path, "pyproject.toml", "[tool.rowx.migrate]\n")
    _write_dirs(
        tmp_path / "migrations", "1_create_users.sql", "CREATE TABLE users (id int4 PRIMARY KEY, name text NOT NULL);"
    )
    _write_dirs(tmp_path, "q.py", EMBEDDED_CLEAN_QUERY)
    return tmp_path


@embedded_only
@pytest.mark.usefixtures("embedded_scratch_project")
def test_a_memory_url_checks_a_database_built_from_the_migrations_and_skips_the_drift_gate(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run_cli(capsys, "check", "q.py", "--database-url", "embedded://") == (0, "", "")
    code, out, err = _run_cli(capsys, "check", "q.py", "--database-url", "embedded://", "--statistics")
    assert (code, err) == (0, "")
    assert "scratch database built in:" in out
    assert "migrations checked" not in out


@embedded_only
@pytest.mark.usefixtures("embedded_scratch_project")
@pytest.mark.parametrize("jobs", ["1", "4"])
def test_a_memory_check_reports_the_same_diagnostic_at_any_job_count(
    jobs: str, embedded_scratch_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_dirs(
        embedded_scratch_project,
        "q.py",
        "from rowx import query\n\n"
        'query[tuple[str]](t"SELECT id FROM users")\n'
        'query[tuple[int]](t"SELECT id FROM users")\n'
        'query[tuple[int]](t"SELECT name FROM users")\n',
    )
    code, out, err = _run_cli(capsys, "check", "q.py", "--database-url", "embedded://", "-j", jobs)
    assert (code, err) == (1, "")
    assert out.count("error[") == 2
    assert "type-mismatch" in out


@embedded_only
def test_a_memory_url_without_migrations_exits_two_before_starting_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_dirs(tmp_path, "pyproject.toml", "")
    _write_dirs(tmp_path, "q.py", EMBEDDED_CLEAN_QUERY)
    code, out, err = _run_cli(capsys, "check", "q.py", "--database-url", "embedded://")
    assert (code, out) == (2, "")
    assert "[tool.rowx.migrate]" in err


@embedded_only
def test_a_failing_migration_exits_two_with_the_server_message(
    embedded_scratch_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_dirs(embedded_scratch_project / "migrations", "2_explode.sql", "SELECT 1/0;")
    code, out, err = _run_cli(capsys, "check", "q.py", "--database-url", "embedded://", "-j", "3")
    assert (code, out) == (2, "")
    assert err.startswith("error: the migrations failed on the scratch database: ")
    assert "division by zero" in err


@embedded_only
@pytest.mark.usefixtures("embedded_scratch_project")
def test_nothing_starts_when_nothing_is_described(
    embedded_scratch_project: Path, private_tmp: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_dirs(embedded_scratch_project, "q.py", "x = 1\n")
    assert _run_cli(capsys, "check", "q.py", "--database-url", "embedded://") == (0, "", "")
    assert list(private_tmp.iterdir()) == []


@pytest.fixture
def embedded_directory_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    _write_dirs(tmp_path, "pyproject.toml", "[tool.rowx.migrate]\n")
    _write_dirs(tmp_path / "migrations", "1_create_drift_a.sql", "CREATE TABLE drift_a (id int4);")
    _write_dirs(tmp_path / "migrations", "2_create_drift_b.sql", "CREATE TABLE drift_b (id int4);")
    _write_dirs(tmp_path, "q.py", EMBEDDED_DRIFT_QUERY)
    return tmp_path


@embedded_only
def test_a_directory_check_runs_the_drift_gate_first_pending_then_clean(
    embedded_directory_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    url = f"embedded://{embedded_directory_project / 'db'}"
    assert _run_cli(capsys, "check", "q.py", "--database-url", url) == (2, "", _golden("drift_pending.txt"))
    assert _run_cli(capsys, "migrate", "run", "--database-url", url)[0] == 0
    # -j 4 is clamped to the single session that the instance serves
    code, out, err = _run_cli(capsys, "check", "q.py", "--database-url", url, "--statistics", "-j", "4")
    assert (code, err) == (0, "")
    assert "migrations checked: 2\n" in out
    assert "scratch database" not in out


@embedded_only
def test_a_directory_check_skips_the_gate_on_request_and_keeps_the_instance_data(
    embedded_directory_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    url = f"embedded://{embedded_directory_project / 'db'}"
    # nothing is migrated, so the query itself is the diagnostic, not the gate
    code, out, err = _run_cli(capsys, "check", "q.py", "--database-url", url, "--no-drift-check")
    assert (code, err) == (1, "")
    assert "SQLSTATE 42P01" in out
    assert (embedded_directory_project / "db").is_dir()


@embedded_only
def test_scratch_on_a_directory_is_a_usage_error(
    embedded_directory_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    url = f"embedded://{embedded_directory_project / 'db'}"
    code, out, err = _run_cli(capsys, "check", "q.py", "--database-url", url, "--scratch")
    assert (code, out) == (2, "")
    assert "directory" in err


@embedded_only
def test_migrate_run_info_revert_on_a_directory_match_the_docker_goldens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_dirs(tmp_path, "pyproject.toml", "")
    directory = tmp_path / "migrations"
    _write_dirs(directory, "1_create_cli_a.sql", "CREATE TABLE cli_a (id int4);")
    _write_dirs(directory, "2_create_cli_b.up.sql", "CREATE TABLE cli_b (id int4);")
    _write_dirs(directory, "2_create_cli_b.down.sql", "DROP TABLE cli_b;")
    url = f"embedded://{tmp_path / 'db'}"

    def cli(*argv: str) -> tuple[int, str, str]:
        code, out, err = _run_cli(capsys, "migrate", *argv, "--database-url", url, "--source", str(directory))
        return code, _normalize(out), err

    assert cli("info") == (0, _golden("migrate_info_pending.txt"), "")
    assert cli("run") == (0, _golden("migrate_run.txt"), "")
    # the data outlives the instance that wrote it
    assert cli("info") == (0, _golden("migrate_info_applied.txt"), "")
    assert cli("revert") == (0, _golden("migrate_revert.txt"), "")
    assert cli("info") == (0, _golden("migrate_info_reverted.txt"), "")
    assert cli("run") == (0, "Applied 2/migrate create cli b (<time>)\n", "")


@embedded_only
def test_a_directory_that_is_in_use_fails_at_once_instead_of_retrying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_dirs(tmp_path, "pyproject.toml", "")
    _write_dirs(tmp_path / "migrations", "1_create_a.sql", "CREATE TABLE a (id int4);")
    directory = tmp_path / "db"
    holder = asyncio.run(embedded.start(directory=directory))
    try:
        started = time.monotonic()
        # a 30 second retry window, which a retried lock error would use up
        code, out, err = _run_cli(
            capsys, "migrate", "info", "--database-url", f"embedded://{directory}", "--connect-timeout", "30"
        )
        elapsed = time.monotonic() - started
    finally:
        asyncio.run(holder.close())
    assert (code, out) == (2, "")
    assert err.startswith("error: could not start the embedded database: ")
    assert "in use by pid" in err
    assert elapsed < RETRY_LIMIT


@embedded_only
def test_migrate_on_a_memory_url_exits_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_dirs(tmp_path, "pyproject.toml", "")
    _write_dirs(tmp_path / "migrations", "1_create_a.sql", "CREATE TABLE a (id int4);")
    code, out, err = _run_cli(capsys, "migrate", "run", "--database-url", "embedded://")
    assert (code, out) == (2, "")
    assert (
        err == "error: an in-memory embedded database is gone when the command exits; use embedded:///absolute/path\n"
    )


@embedded_only
@pytest.mark.parametrize(
    "scenario",
    [(command, directory) for command in ("create", "drop", "setup", "reset") for directory in (False, True)],
)
def test_database_commands_exit_two_on_an_embedded_url_without_starting_it(
    scenario: tuple[str, bool],
    tmp_path: Path,
    private_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    command, directory = scenario
    monkeypatch.chdir(tmp_path)
    _write_dirs(tmp_path, "pyproject.toml", "")
    target = tmp_path / "db"
    url = f"embedded://{target}" if directory else "embedded://"
    code, out, err = _run_cli(capsys, "database", command, "--database-url", url)
    assert (code, out) == (2, "")
    assert err == "error: `rowx database` is not supported for embedded URLs\n"
    # nothing was started: no directory was made, and no instance left a socket directory
    assert not target.exists()
    assert list(private_tmp.iterdir()) == []


SLOW_SECONDS: Final[int] = 30
# the first boot of the engine in a process takes up to about two seconds, several times that on a loaded machine
BOOT_SECONDS: Final[float] = 8.0
PROMPT_SECONDS: Final[float] = 5.0
# from the instance being up to the migration running on it
CONNECT_SECONDS: Final[float] = 4.0


def _start_rowx(project: Path, private_tmp: Path, stderr: Path, *argv: str) -> subprocess.Popen[str]:
    rowx = Path(sys.executable).parent / "rowx"
    # the child keeps its own descriptor, so the file is closed here
    with stderr.open("w", encoding="utf-8") as sink:
        return subprocess.Popen(  # ruff: ignore[subprocess-without-shell-equals-true]
            [str(rowx), *argv],
            cwd=project,
            env={**os.environ, "TMPDIR": str(private_tmp)},
            stdout=subprocess.DEVNULL,
            stderr=sink,
            text=True,
        )


def _slow_project(tmp_path: Path, seconds: int) -> Path:
    _write_dirs(tmp_path, "pyproject.toml", "[tool.rowx.migrate]\n")
    _write_dirs(
        tmp_path / "migrations", "1_slow.sql", f"CREATE TABLE t (id int4 NOT NULL); SELECT pg_sleep({seconds});"
    )
    _write_dirs(tmp_path, "q.py", 'from rowx import query\n\nquery[tuple[int]](t"SELECT id FROM t")\n')
    return tmp_path


def _until(condition: Callable[[], bool], proc: subprocess.Popen[str], what: str) -> None:
    # each poll is a directory listing or a file read, so no sleep is needed
    deadline = time.monotonic() + RETRY_LIMIT * 3
    while not condition() and proc.poll() is None and time.monotonic() < deadline:
        pass
    assert proc.poll() is None, f"the run ended before {what}"
    assert condition(), f"{what} never happened"


def _running_migration(proc: subprocess.Popen[str], private_tmp: Path, directory: Path | None = None) -> None:
    if directory is not None:
        # a directory records its owner beside it as soon as the instance is up, which the run then connects to
        owner = directory.parent / f".{directory.name}.rowx-owner"
        _until(owner.exists, proc, "the instance started")
        wait = CONNECT_SECONDS
    else:
        # a memory instance has no such record: its root exists once it has started, and the run is then given the
        # time of a first boot, which a `pg_sleep` far longer than that keeps from being over
        _until(
            lambda: any(entry.name.startswith("rxe-") for entry in private_tmp.iterdir()),
            proc,
            "the instance started",
        )
        wait = BOOT_SECONDS
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        pass


@embedded_only
def test_sigint_mid_migration_exits_two_at_once_and_leaves_nothing_of_a_memory_database(
    tmp_path: Path, private_tmp: Path
) -> None:
    project = _slow_project(tmp_path, SLOW_SECONDS)
    err_file = tmp_path / "stderr.txt"
    started = time.monotonic()
    proc = _start_rowx(project, private_tmp, err_file, "check", "q.py", "--database-url", "embedded://", "-j", "3")
    try:
        _running_migration(proc, private_tmp)
        proc.send_signal(signal.SIGINT)
        signalled = time.monotonic()
        proc.wait(timeout=RETRY_LIMIT * 3)
        exited = time.monotonic()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert proc.returncode == 2
    assert err_file.read_text(encoding="utf-8") == "error: interrupted\n"
    # promptly, though the engine is still inside a statement that has most of its 30 seconds to go
    assert exited - signalled < PROMPT_SECONDS
    assert exited - started < SLOW_SECONDS - 5
    assert list(private_tmp.iterdir()) == []


@embedded_only
def test_the_first_sigint_on_a_directory_says_it_is_waiting_and_exits_two_after_the_statement(
    tmp_path: Path, private_tmp: Path
) -> None:
    seconds = 8
    project = _slow_project(tmp_path, seconds)
    err_file = tmp_path / "stderr.txt"
    url = f"embedded://{tmp_path / 'db'}"
    proc = _start_rowx(project, private_tmp, err_file, "migrate", "run", "--database-url", url)
    try:
        _running_migration(proc, private_tmp, tmp_path / "db")
        proc.send_signal(signal.SIGINT)
        signalled = time.monotonic()
        # the message is immediate, long before the statement is over
        _until(lambda: "waiting for the running statement" in err_file.read_text(encoding="utf-8"), proc, "the message")
        assert time.monotonic() - signalled < PROMPT_SECONDS
        proc.wait(timeout=RETRY_LIMIT * 3)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert proc.returncode == 2
    err = err_file.read_text(encoding="utf-8")
    assert "press Ctrl-C again to abort" in err
    assert err.endswith("error: interrupted\n")
    # the close waited for the statement, and left the directory consistent
    assert list(private_tmp.iterdir()) == []


@embedded_only
def test_the_second_sigint_on_a_directory_aborts_at_once_and_the_directory_opens_again(
    tmp_path: Path, private_tmp: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _slow_project(tmp_path, SLOW_SECONDS)
    err_file = tmp_path / "stderr.txt"
    url = f"embedded://{tmp_path / 'db'}"
    started = time.monotonic()
    proc = _start_rowx(project, private_tmp, err_file, "migrate", "run", "--database-url", url)
    try:
        _running_migration(proc, private_tmp, tmp_path / "db")
        proc.send_signal(signal.SIGINT)
        _until(lambda: "waiting for the running statement" in err_file.read_text(encoding="utf-8"), proc, "the message")
        proc.send_signal(signal.SIGINT)
        second = time.monotonic()
        proc.wait(timeout=RETRY_LIMIT * 3)
        exited = time.monotonic()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert proc.returncode == 2
    assert exited - second < PROMPT_SECONDS
    assert exited - started < SLOW_SECONDS - 5
    assert err_file.read_text(encoding="utf-8").endswith("error: interrupted\n")
    # no socket directory survives (the engine's own scratch files are swept by a later start)
    assert [entry.name for entry in private_tmp.iterdir() if entry.name.startswith("rxe-")] == []
    # the abandoned directory is crash-safe: it opens again, with the interrupted migration not applied
    monkeypatch.chdir(project)
    monkeypatch.setenv("TMPDIR", str(private_tmp))
    code, out, err = _run_cli(capsys, "migrate", "info", "--database-url", url)
    assert (code, err) == (0, "")
    assert out == "1/pending slow\n"


@embedded_only
@pytest.mark.parametrize(
    "scenario",
    [
        ("CREATE TABLE t (id int4 NOT NULL);", 0, ""),
        ("CREATE TABLE t (id text);", 1, "type-mismatch"),
        ("SELECT 1/0;", 2, "division by zero"),
    ],
    ids=["clean", "diagnostic", "error"],
)
def test_no_instance_socket_directory_or_temp_root_survives_an_exit(
    scenario: tuple[str, int, str], tmp_path: Path, private_tmp: Path
) -> None:
    migration, code, needle = scenario
    project = _slow_project(tmp_path, 0)
    _write_dirs(project / "migrations", "1_slow.sql", migration)
    out_file = tmp_path / "stderr.txt"
    proc = _start_rowx(project, private_tmp, out_file, "check", "q.py", "--database-url", "embedded://", "-j", "3")
    proc.wait(timeout=RETRY_LIMIT * 3)
    assert proc.returncode == code, out_file.read_text(encoding="utf-8")
    # a diagnostic is on stdout, which the helper discards, so only the failure message is looked for
    if code == 2:
        assert needle in out_file.read_text(encoding="utf-8")
    assert list(private_tmp.iterdir()) == []
