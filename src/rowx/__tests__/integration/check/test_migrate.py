import re
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Final

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from rowx.check.cli import main

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = [pytest.mark.integration, pytest.mark.db]

GOLDEN: Final[Path] = Path(__file__).resolve().parent / "golden"
ELAPSED: Final = re.compile(r"\(\d+\.\d(?:us|ms|s)\)")


def golden(name: str) -> str:
    return (GOLDEN / name).read_text(encoding="utf-8")


def normalize(text: str) -> str:
    return ELAPSED.sub("(<time>)", text)


def write(directory: Path, name: str, text: str) -> None:
    directory.mkdir(exist_ok=True)
    (directory / name).write_text(text, encoding="utf-8")


def cli(capsys: pytest.CaptureFixture[str], dsn: str, directory: Path, *argv: str) -> tuple[int, str, str]:
    code = main(["migrate", *argv, "--database-url", dsn, "--source", str(directory)])
    captured = capsys.readouterr()
    return code, normalize(captured.out), captured.err


def exists(dsn: str, name: str) -> bool:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute("SELECT to_regclass(%s) IS NOT NULL", (name,)).fetchone()
    return bool(row and row[0])


def applied_versions(dsn: str) -> list[int]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return [row[0] for row in conn.execute("SELECT version FROM _rowx_migrations ORDER BY version")]


@pytest.fixture
def migrations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    directory = tmp_path / "migrations"
    write(directory, "1_create_cli_a.sql", "CREATE TABLE cli_a (id int4);")
    write(directory, "2_create_cli_b.up.sql", "CREATE TABLE cli_b (id int4);")
    write(directory, "2_create_cli_b.down.sql", "DROP TABLE cli_b;")
    return directory


def test_run_info_revert_info_match_goldens(schema: str, migrations: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli(capsys, schema, migrations, "info") == (0, golden("migrate_info_pending.txt"), "")
    assert not exists(schema, "_rowx_migrations")

    assert cli(capsys, schema, migrations, "run") == (0, golden("migrate_run.txt"), "")
    assert exists(schema, "cli_a")
    assert exists(schema, "cli_b")
    assert cli(capsys, schema, migrations, "info") == (0, golden("migrate_info_applied.txt"), "")

    # without a target, only the latest migration is reverted
    assert cli(capsys, schema, migrations, "revert") == (0, golden("migrate_revert.txt"), "")
    assert exists(schema, "cli_a")
    assert not exists(schema, "cli_b")
    assert cli(capsys, schema, migrations, "info") == (0, golden("migrate_info_reverted.txt"), "")

    assert cli(capsys, schema, migrations, "run") == (0, "Applied 2/migrate create cli b (<time>)\n", "")
    assert cli(capsys, schema, migrations, "run") == (0, "", "")


def test_run_dry_run_changes_nothing(schema: str, migrations: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli(capsys, schema, migrations, "run", "--dry-run") == (0, golden("migrate_dry_run.txt"), "")
    assert not exists(schema, "_rowx_migrations")
    assert not exists(schema, "cli_a")
    assert not exists(schema, "cli_b")


def test_revert_dry_run_changes_nothing(schema: str, migrations: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cli(capsys, schema, migrations, "run")
    assert cli(capsys, schema, migrations, "revert", "--dry-run") == (0, golden("migrate_dry_revert.txt"), "")
    assert exists(schema, "cli_b")
    assert applied_versions(schema) == [1, 2]
    assert cli(capsys, schema, migrations, "revert", "--target-version", "0", "--dry-run") == (
        2,
        "",
        "error: migration 1 has no .down.sql file to revert it\n",
    )
    assert applied_versions(schema) == [1, 2]


def test_revert_dry_run_on_a_fresh_database_creates_nothing(
    schema: str,
    migrations: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli(capsys, schema, migrations, "revert", "--dry-run") == (0, "", "")
    assert cli(capsys, schema, migrations, "revert") == (0, "", "")
    assert not exists(schema, "cli_a")


def test_dry_run_reports_the_same_errors_as_a_real_run(
    schema: str,
    migrations: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli(capsys, schema, migrations, "run")
    write(migrations, "1_create_cli_a.sql", "CREATE TABLE cli_a (id int8);")
    code, out, err = cli(capsys, schema, migrations, "run", "--dry-run")
    assert (code, out) == (2, "")
    assert err == "error: migration 1 was modified after it was applied\n"


def test_target_version(schema: str, migrations: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write(migrations, "3_create_cli_c.sql", "CREATE TABLE cli_c (id int4);")
    assert cli(capsys, schema, migrations, "run", "--target-version", "2", "--dry-run") == (
        0,
        golden("migrate_dry_run.txt"),
        "",
    )
    assert cli(capsys, schema, migrations, "run", "--target-version", "2")[0] == 0
    assert applied_versions(schema) == [1, 2]
    assert not exists(schema, "cli_c")

    cli(capsys, schema, migrations, "run")
    assert applied_versions(schema) == [1, 2, 3]
    (migrations / "3_create_cli_c.sql").unlink()
    write(migrations, "3_create_cli_c.up.sql", "CREATE TABLE cli_c (id int4);")
    write(migrations, "3_create_cli_c.down.sql", "DROP TABLE cli_c;")
    code, out, _ = cli(capsys, schema, migrations, "revert", "--target-version", "1")
    assert code == 0
    assert out == "Reverted 3/revert create cli c (<time>)\nReverted 2/revert create cli b (<time>)\n"
    assert applied_versions(schema) == [1]

    code, out, _ = cli(capsys, schema, migrations, "revert", "--target-version", "0")
    assert code == 2
    assert not out


def test_revert_to_zero_reverts_everything(schema: str, migrations: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write(migrations, "1_create_cli_a.up.sql", "CREATE TABLE cli_a (id int4);")
    write(migrations, "1_create_cli_a.down.sql", "DROP TABLE cli_a;")
    (migrations / "1_create_cli_a.sql").unlink()
    cli(capsys, schema, migrations, "run")
    code, out, _ = cli(capsys, schema, migrations, "revert", "--target-version", "0")
    assert code == 0
    assert out.splitlines() == ["Reverted 2/revert create cli b (<time>)", "Reverted 1/revert create cli a (<time>)"]
    assert applied_versions(schema) == []


def test_revert_without_a_down_file_exits_two_and_reverts_nothing(
    schema: str,
    migrations: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli(capsys, schema, migrations, "run")
    code, out, err = cli(capsys, schema, migrations, "revert", "--target-version", "0")
    assert (code, out) == (2, "")
    assert err == "error: migration 1 has no .down.sql file to revert it\n"
    assert applied_versions(schema) == [1, 2]


def test_out_of_order_exits_two_unless_allowed(
    schema: str,
    migrations: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli(capsys, schema, migrations, "run")
    write(migrations, "15_late.sql", "CREATE TABLE cli_late (id int4);")
    cli(capsys, schema, migrations, "run")
    write(migrations, "4_behind.sql", "SELECT 1;")
    code, out, err = cli(capsys, schema, migrations, "run")
    assert (code, out) == (2, "")
    assert err == "error: migration 4 is older than the latest applied migration 15\n"
    code, out, err = cli(capsys, schema, migrations, "run", "--dry-run")
    assert (code, out) == (2, "")
    assert "older than the latest applied migration 15" in err

    assert cli(capsys, schema, migrations, "run", "--allow-out-of-order", "--dry-run") == (
        0,
        "Would apply 4/migrate behind\n",
        "",
    )
    assert cli(capsys, schema, migrations, "run", "--allow-out-of-order") == (
        0,
        "Applied 4/migrate behind (<time>)\n",
        "",
    )
    assert applied_versions(schema) == [1, 2, 4, 15]


def test_missing_migrations_exit_two_unless_ignored(
    schema: str,
    migrations: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli(capsys, schema, migrations, "run")
    (migrations / "1_create_cli_a.sql").unlink()
    write(migrations, "3_create_cli_c.sql", "CREATE TABLE cli_c (id int4);")
    code, _, err = cli(capsys, schema, migrations, "run")
    assert code == 2
    assert err == "error: migration 1 is applied but missing from the migration directory\n"
    assert cli(capsys, schema, migrations, "run", "--ignore-missing", "--dry-run") == (
        0,
        "Would apply 3/migrate create cli c\n",
        "",
    )
    assert cli(capsys, schema, migrations, "run", "--ignore-missing")[0] == 0
    assert "1/missing" in cli(capsys, schema, migrations, "info")[1]


def test_info_shows_both_checksums_of_a_modified_migration(
    schema: str,
    migrations: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli(capsys, schema, migrations, "run")
    write(migrations, "1_create_cli_a.sql", "CREATE TABLE cli_a (id int8);")
    code, out, err = cli(capsys, schema, migrations, "info")
    assert (code, err) == (0, "")
    first, second = out.splitlines()
    assert re.fullmatch(r"1/modified create cli a \(local [0-9a-f]{8}, applied [0-9a-f]{8}\)", first)
    local, applied = re.findall(r"[0-9a-f]{8}", first.split("(", 1)[1])
    assert local != applied
    assert second == "2/applied create cli b (<time>)"


@pytest.mark.parametrize(
    ("table", "stored"),
    [("Rowx_History", "rowx_history"), ('"Rowx ""History"""', 'Rowx "History"')],
)
def test_info_reads_the_applied_checksum_from_the_parsed_history_table(
    schema: str,
    migrations: Path,
    capsys: pytest.CaptureFixture[str],
    table: str,
    stored: str,
) -> None:
    (migrations.parent / "pyproject.toml").write_text(f"[tool.rowx.migrate]\ntable = '{table}'\n", encoding="utf-8")
    assert cli(capsys, schema, migrations, "run")[0] == 0
    with psycopg.connect(schema, autocommit=True) as conn:
        query = "SELECT tablename FROM pg_tables WHERE schemaname = current_schema() AND tablename ILIKE '%history%'"
        tables = [row[0] for row in conn.execute(query)]
    # one history table, under the name the server folded or unquoted
    assert tables == [stored]
    write(migrations, "1_create_cli_a.sql", "CREATE TABLE cli_a (id int8);")
    code, out, err = cli(capsys, schema, migrations, "info")
    assert (code, err) == (0, "")
    assert re.fullmatch(r"1/modified create cli a \(local [0-9a-f]{8}, applied [0-9a-f]{8}\)", out.splitlines()[0])


def test_a_failing_migration_exits_two_with_the_server_message(
    schema: str,
    migrations: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write(migrations, "3_broken.sql", "SELECT * FROM cli_does_not_exist;")
    code, out, err = cli(capsys, schema, migrations, "run")
    assert code == 2
    assert not out
    assert err.startswith("error: ")
    assert "cli_does_not_exist" in err
    # the migrations before the broken one stay applied
    assert applied_versions(schema) == [1, 2]


def test_config_table_and_create_schemas_are_used(
    schema: str,
    migrations: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    name = f"cfg_{secrets.token_hex(4)}"
    (migrations.parent / "pyproject.toml").write_text(
        f'[tool.rowx.migrate]\ntable = "{name}.history"\ncreate_schemas = ["{name}"]\n',
        encoding="utf-8",
    )
    try:
        # a dry run creates neither the schema nor the table
        assert cli(capsys, schema, migrations, "run", "--dry-run") == (0, golden("migrate_dry_run.txt"), "")
        assert not exists(schema, f"{name}.history")
        with psycopg.connect(schema, autocommit=True) as conn:
            assert conn.execute("SELECT to_regnamespace(%s)", (name,)).fetchone() == (None,)
        assert cli(capsys, schema, migrations, "run")[0] == 0
        assert exists(schema, f"{name}.history")
        assert not exists(schema, "_rowx_migrations")
        assert cli(capsys, schema, migrations, "info")[1] == golden("migrate_info_applied.txt")
    finally:
        with psycopg.connect(schema, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name)))


@pytest.fixture
def limited_dsn(schema: str) -> Iterator[tuple[str, str]]:
    role = f"rowx_pw_{secrets.token_hex(4)}"
    password = f"hunter2-{secrets.token_hex(6)}"
    schema_name = str(conninfo_to_dict(schema)["options"]).rsplit("=", 1)[1]
    with psycopg.connect(schema, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(role), sql.Literal(password)),
        )
        conn.execute(sql.SQL("GRANT ALL ON SCHEMA {} TO {}").format(sql.Identifier(schema_name), sql.Identifier(role)))
        try:
            yield make_conninfo(schema, user=role, password=password), password
        finally:
            conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def test_a_password_is_never_printed(
    limited_dsn: tuple[str, str],
    migrations: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dsn, password = limited_dsn
    seen: list[str] = []
    for argv in (["run", "--dry-run"], ["run"], ["info"], ["revert", "--dry-run"], ["revert"], ["info"]):
        code, out, err = cli(capsys, dsn, migrations, *argv)
        assert code == 0, err
        seen.extend((out, err))
    # a failure is reported without it too
    write(migrations, "3_broken.sql", f"-- {password}\nSELECT * FROM cli_does_not_exist;")
    code, out, err = cli(capsys, dsn, migrations, "run")
    assert code == 2
    seen.extend((out, err))
    # and so is a refused login
    wrong = make_conninfo(dsn, password=f"{password}-wrong", host="127.0.0.1")
    code, out, err = cli(capsys, wrong, migrations, "info")
    assert code == 2
    seen.extend((out, err))
    assert all(password not in text for text in seen)
