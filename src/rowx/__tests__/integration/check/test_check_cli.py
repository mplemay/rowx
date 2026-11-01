import asyncio
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Final

import psycopg
import pytest

from rowx import query_unchecked
from rowx.__tests__.integration.check.harness import CASES_DIR, check_case, parse_markers
from rowx._core.postgres import connect_checker
from rowx.check.cli import main
from rowx.check.drift import read_history
from rowx.errors import DatabaseError
from rowx.postgres import Connection, Migrator

if TYPE_CHECKING:
    from rowx.__tests__.integration.check.harness import Expectation

pytestmark = [pytest.mark.integration, pytest.mark.db]

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
            statuses = await read_history(migrator, conn)
            assert [s.state for s in statuses] == ["pending", "pending"]
            # the same session refuses a write
            with pytest.raises(DatabaseError) as raised:
                await query_unchecked[None](t"CREATE TABLE drift_nope (id int4)").execute(conn)
            assert raised.value.sqlstate == "25006"
        finally:
            await conn.close()

    asyncio.run(run())
