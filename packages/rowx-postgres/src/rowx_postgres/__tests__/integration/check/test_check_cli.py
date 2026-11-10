import asyncio
import json
import os
import secrets
import signal
import subprocess  # ruff: ignore[suspicious-subprocess-import]
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote, urlparse, urlunparse

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from rowx_core.errors import DatabaseError
from rowx_postgres import Connection, Migrator
from rowx_postgres.__tests__.integration.check.harness import CASES_DIR, check_case, parse_markers
from rowx_postgres._core import connect_checker
from rowx_postgres._services import PostgresMigrationSession

from rowx import query_unchecked
from rowx.check.cli import main
from rowx.check.drift import read_history
from rowx.check.runner import CheckError, describe_all as describe_all_impl

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from rowx_postgres.__tests__.integration.check.harness import Expectation

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
def test_scratch_clean_run_when_the_target_lacks_the_tables(
    no_leftovers: str, capsys: pytest.CaptureFixture[str]
) -> None:
    # the drift gate would reject this database, which has no history table
    assert _drift(capsys, no_leftovers)[0] == 2
    assert _scratch(capsys, no_leftovers) == (0, "", "")


@pytest.mark.usefixtures("scratch_project")
def test_scratch_reports_diagnostics_against_the_scratch_schema(
    no_leftovers: str, scratch_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(scratch_project, "q.py", SCRATCH_DIAGNOSTIC_QUERY)
    code, out, err = _scratch(capsys, no_leftovers)
    assert (code, err) == (1, "")
    assert "q.py:3" in out
    assert "nullable" in out


@pytest.mark.usefixtures("scratch_project")
def test_scratch_never_touches_the_target_database(no_leftovers: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert _scratch(capsys, no_leftovers)[0] == 0
    with psycopg.connect(no_leftovers, autocommit=True) as conn:
        row = conn.execute("SELECT to_regclass('scratch_users'), to_regclass('_rowx_migrations')").fetchone()
    assert row == (None, None)


def test_scratch_failing_migration_exits_two(
    no_leftovers: str, scratch_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(scratch_project / "migrations", "2_explode.sql", "SELECT 1/0;")
    code, out, err = _scratch(capsys, no_leftovers)
    assert (code, out) == (2, "")
    assert "2" in err
    assert "division by zero" in err


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

    async def describe_all(dsn: str, *args: Any) -> dict[str, Outcome]:  # ruff: ignore[any-type]
        conn = Connection(await connect_checker(dsn, 10.0))
        try:
            tables.append(
                await query_unchecked[tuple[str | None, str | None]](
                    t"SELECT to_regclass('rowx_meta.history')::text, to_regclass('public._rowx_migrations')::text"
                ).one(conn)
            )
        finally:
            await conn.close()
        return await original(dsn, *args)

    monkeypatch.setattr("rowx.check.runner.describe_all", describe_all)
    assert _scratch(capsys, no_leftovers) == (0, "", "")
    assert tables == [("rowx_meta.history", None)]


@pytest.mark.usefixtures("scratch_project")
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

    async def describe_all(dsn: str, *args: Any) -> dict[str, Outcome]:  # ruff: ignore[any-type]
        conn = Connection(await connect_checker(dsn, 10.0))
        try:
            described.append(await query_unchecked[str](t"SELECT current_database()::text").one(conn))
        finally:
            await conn.close()
        return await original(dsn, *args)

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

    async def describe_all(dsn: str, *_: object) -> dict[str, Outcome]:
        seen.append(dsn)
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
def test_scratch_check_session_is_read_only(
    no_leftovers: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    states: list[str] = []
    original = describe_all_impl

    async def describe_all(dsn: str, *args: Any) -> dict[str, Outcome]:  # ruff: ignore[any-type]
        conn = Connection(await connect_checker(dsn, 10.0))
        try:
            states.append(await query_unchecked[str](t"SHOW default_transaction_read_only").one(conn))
        finally:
            await conn.close()
        return await original(dsn, *args)

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
