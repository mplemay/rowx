import json
import re
import secrets
import socket
import threading
from contextlib import closing, contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Final

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from rowx.check.cli import main

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterator

    from rowx.postgres import ConnectOptions

pytestmark = [pytest.mark.integration, pytest.mark.db]

GOLDEN: Final[Path] = Path(__file__).resolve().parent / "golden"
ELAPSED: Final = re.compile(r"\(\d+\.\d(?:us|ms|s)\)")
JSON_TIMES: Final = re.compile(r'("installed_on": )"[^"]+"|("execution_time": )\d+')
WAIT_LIMIT: Final[float] = 30.0


def golden(name: str) -> str:
    return (GOLDEN / name).read_text(encoding="utf-8")


def normalize(text: str) -> str:
    text = ELAPSED.sub("(<time>)", text)
    return JSON_TIMES.sub(lambda m: f'{m[1] or m[2]}"<time>"', text)


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
    assert cli(capsys, schema, migrations, "revert", "--all", "--dry-run") == (
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

    code, out, _ = cli(capsys, schema, migrations, "revert", "--all")
    assert code == 2
    assert not out


def test_revert_target_zero_is_an_unknown_version_not_everything(
    schema: str, migrations: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli(capsys, schema, migrations, "run")
    assert cli(capsys, schema, migrations, "revert", "--target-version", "0") == (
        2,
        "",
        "error: target migration 0 does not exist\n",
    )
    assert applied_versions(schema) == [1, 2]


def test_revert_all_after_a_reversible_pair_replaces_the_old_target_zero(
    schema: str, migrations: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(migrations, "1_create_cli_a.up.sql", "CREATE TABLE cli_a (id int4);")
    write(migrations, "1_create_cli_a.down.sql", "DROP TABLE cli_a;")
    (migrations / "1_create_cli_a.sql").unlink()
    cli(capsys, schema, migrations, "run")
    code, out, _ = cli(capsys, schema, migrations, "revert", "--all")
    assert code == 0
    assert out.splitlines() == ["Reverted 2/revert create cli b (<time>)", "Reverted 1/revert create cli a (<time>)"]
    assert applied_versions(schema) == []


def test_revert_without_a_down_file_exits_two_and_reverts_nothing(
    schema: str,
    migrations: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli(capsys, schema, migrations, "run")
    code, out, err = cli(capsys, schema, migrations, "revert", "--all")
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


def test_skip_then_info_match_goldens(schema: str, migrations: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli(capsys, schema, migrations, "skip", "--dry-run") == (
        0,
        "Would skip 1/skip create cli a\nWould skip 2/skip create cli b\n",
        "",
    )
    assert not exists(schema, "_rowx_migrations")

    assert cli(capsys, schema, migrations, "skip") == (0, golden("migrate_skip.txt"), "")
    # recorded, but nothing ran
    assert applied_versions(schema) == [1, 2]
    assert not exists(schema, "cli_a")
    assert not exists(schema, "cli_b")
    # a skipped migration has no execution time
    assert cli(capsys, schema, migrations, "info") == (0, "1/applied create cli a\n2/applied create cli b\n", "")
    assert cli(capsys, schema, migrations, "run") == (0, "", "")
    assert cli(capsys, schema, migrations, "skip") == (0, "", "")


def test_skip_to_a_target_leaves_the_rest_pending(
    schema: str, migrations: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli(capsys, schema, migrations, "skip", "--target-version", "1") == (0, "Skipped 1/skip create cli a\n", "")
    assert cli(capsys, schema, migrations, "run") == (0, "Applied 2/migrate create cli b (<time>)\n", "")
    assert not exists(schema, "cli_a")
    assert exists(schema, "cli_b")


def test_skip_follows_the_out_of_order_and_missing_rules(
    schema: str, migrations: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(migrations, "9_late.sql", "SELECT 1;")
    cli(capsys, schema, migrations, "skip", "--target-version", "9")
    write(migrations, "5_behind.sql", "SELECT 1;")
    code, out, err = cli(capsys, schema, migrations, "skip")
    assert (code, out) == (2, "")
    assert err == "error: migration 5 is older than the latest applied migration 9\n"
    assert cli(capsys, schema, migrations, "skip", "--allow-out-of-order") == (0, "Skipped 5/skip behind\n", "")
    (migrations / "9_late.sql").unlink()
    assert cli(capsys, schema, migrations, "skip")[0] == 2
    assert cli(capsys, schema, migrations, "skip", "--ignore-missing") == (0, "", "")


def test_revert_all_reverts_everything_and_conflicts_with_a_target(
    schema: str, migrations: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(migrations, "1_create_cli_a.up.sql", "CREATE TABLE cli_a (id int4);")
    write(migrations, "1_create_cli_a.down.sql", "DROP TABLE cli_a;")
    (migrations / "1_create_cli_a.sql").unlink()
    cli(capsys, schema, migrations, "run")

    with pytest.raises(SystemExit) as exit_info:
        cli(capsys, schema, migrations, "revert", "--all", "--target-version", "1")
    assert exit_info.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err
    assert applied_versions(schema) == [1, 2]

    assert cli(capsys, schema, migrations, "revert", "--all", "--dry-run") == (
        0,
        "Would revert 2/revert create cli b\nWould revert 1/revert create cli a\n",
        "",
    )
    assert applied_versions(schema) == [1, 2]
    assert cli(capsys, schema, migrations, "revert", "--all") == (
        0,
        "Reverted 2/revert create cli b (<time>)\nReverted 1/revert create cli a (<time>)\n",
        "",
    )
    assert applied_versions(schema) == []
    assert not exists(schema, "cli_a")


def test_revert_all_stops_before_a_simple_migration(
    schema: str, migrations: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli(capsys, schema, migrations, "run")
    code, out, err = cli(capsys, schema, migrations, "revert", "--all")
    assert (code, out) == (2, "")
    assert err == "error: migration 1 has no .down.sql file to revert it\n"
    assert applied_versions(schema) == [1, 2]


def test_revert_ignore_missing_covers_only_versions_outside_the_range(
    schema: str, migrations: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(migrations, "3_create_cli_c.up.sql", "CREATE TABLE cli_c (id int4);")
    write(migrations, "3_create_cli_c.down.sql", "DROP TABLE cli_c;")
    cli(capsys, schema, migrations, "run")
    (migrations / "1_create_cli_a.sql").unlink()

    code, out, err = cli(capsys, schema, migrations, "revert")
    assert (code, out) == (2, "")
    assert err == "error: migration 1 is applied but missing from the migration directory\n"
    assert applied_versions(schema) == [1, 2, 3]

    assert cli(capsys, schema, migrations, "revert", "--ignore-missing") == (
        0,
        "Reverted 3/revert create cli c (<time>)\n",
        "",
    )
    assert applied_versions(schema) == [1, 2]

    # a missing version in the range still fails, even with the flag
    code, out, err = cli(capsys, schema, migrations, "revert", "--all", "--ignore-missing")
    assert (code, out) == (2, "")
    assert "missing" in err
    assert applied_versions(schema) == [1, 2]


def test_info_json_matches_the_golden(schema: str, migrations: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cli(capsys, schema, migrations, "run")
    write(migrations, "2_create_cli_b.up.sql", "CREATE TABLE cli_b (id int8);")
    write(migrations, "3_create_cli_c.sql", "SELECT 1;")
    (migrations / "1_create_cli_a.sql").unlink()
    code, out, err = cli(capsys, schema, migrations, "info", "--output-format", "json")
    assert (code, err) == (0, "")
    assert out == golden("migrate_info_json.txt")

    # the raw output: ISO timestamps, execution time in nanoseconds, checksums in hex
    code = main(["migrate", "info", "--output-format", "json", "--database-url", schema, "--source", str(migrations)])
    assert code == 0
    rows = json.loads(capsys.readouterr().out)
    assert [(row["version"], row["state"]) for row in rows] == [(1, "missing"), (2, "modified"), (3, "pending")]
    assert rows[1]["installed_on"].endswith("+00:00")
    assert isinstance(rows[1]["execution_time"], int)
    # SHA-384 in hex
    assert all(len(row["local_checksum"]) == 96 for row in rows[1:])
    assert rows[0]["local_checksum"] is None


def test_info_json_of_a_fresh_database_lists_pending_migrations_and_an_empty_directory_gives_an_empty_array(
    schema: str, migrations: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = cli(capsys, schema, migrations, "info", "--output-format", "json")
    assert (code, err) == (0, "")
    rows = json.loads(out)
    assert [row["state"] for row in rows] == ["pending", "pending"]
    assert {row["installed_on"] for row in rows} == {None}
    assert {row["execution_time"] for row in rows} == {None}
    assert {row["applied_checksum"] for row in rows} == {None}
    assert not exists(schema, "_rowx_migrations")
    (migrations / "1_create_cli_a.sql").unlink()
    (migrations / "2_create_cli_b.up.sql").unlink()
    (migrations / "2_create_cli_b.down.sql").unlink()
    assert cli(capsys, schema, migrations, "info", "--output-format", "json") == (0, "[]\n", "")


def test_config_allow_out_of_order_is_overridden_by_its_flag(
    schema: str, migrations: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli(capsys, schema, migrations, "run")
    write(migrations, "15_late.sql", "SELECT 1;")
    cli(capsys, schema, migrations, "run")
    write(migrations, "4_behind.sql", "SELECT 1;")

    # the default is off
    assert cli(capsys, schema, migrations, "run", "--dry-run")[0] == 2
    (migrations.parent / "pyproject.toml").write_text(
        "[tool.rowx.migrate]\nallow_out_of_order = true\n", encoding="utf-8"
    )
    assert cli(capsys, schema, migrations, "run", "--dry-run") == (0, "Would apply 4/migrate behind\n", "")
    code, out, err = cli(capsys, schema, migrations, "run", "--no-allow-out-of-order")
    assert (code, out) == (2, "")
    assert "older than the latest applied migration 15" in err
    assert cli(capsys, schema, migrations, "skip", "--dry-run") == (0, "Would skip 4/skip behind\n", "")
    assert cli(capsys, schema, migrations, "run") == (0, "Applied 4/migrate behind (<time>)\n", "")


def test_config_ignore_missing_is_overridden_by_its_flag(
    schema: str, migrations: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli(capsys, schema, migrations, "run")
    (migrations / "1_create_cli_a.sql").unlink()
    write(migrations, "3_create_cli_c.sql", "SELECT 1;")

    assert cli(capsys, schema, migrations, "run", "--dry-run")[0] == 2
    (migrations.parent / "pyproject.toml").write_text("[tool.rowx.migrate]\nignore_missing = true\n", encoding="utf-8")
    assert cli(capsys, schema, migrations, "run", "--dry-run") == (0, "Would apply 3/migrate create cli c\n", "")
    code, out, err = cli(capsys, schema, migrations, "run", "--no-ignore-missing")
    assert (code, out) == (2, "")
    assert err == "error: migration 1 is applied but missing from the migration directory\n"
    assert cli(capsys, schema, migrations, "run") == (0, "Applied 3/migrate create cli c (<time>)\n", "")


# a port that was just free: connecting to it is refused until something listens on it
def free_port() -> int:
    with closing(socket.socket()) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


# nothing listens on its port until `start` is called; then it forwards to a real server
class Proxy:
    def __init__(self, upstream: tuple[str, int]) -> None:
        self.upstream = upstream
        self.listener = socket.socket()
        self.port = free_port()
        self.sockets: list[socket.socket] = []
        self.threads: list[threading.Thread] = []

    def start(self) -> None:
        self.listener.bind(("127.0.0.1", self.port))
        self.listener.listen()
        self.spawn(self.accept)

    def spawn(self, target: Callable[..., None], *args: socket.socket) -> None:
        thread = threading.Thread(target=target, args=args, daemon=True)
        self.threads.append(thread)
        thread.start()

    def accept(self) -> None:
        while True:
            try:
                client, _ = self.listener.accept()
                server = socket.create_connection(self.upstream)
            except OSError:
                return
            self.sockets.extend((client, server))
            self.spawn(self.pump, client, server)
            self.spawn(self.pump, server, client)

    @staticmethod
    def pump(source: socket.socket, sink: socket.socket) -> None:
        with suppress(OSError):
            while data := source.recv(65536):
                sink.sendall(data)
        with suppress(OSError):
            sink.shutdown(socket.SHUT_WR)

    def close(self) -> None:
        for sock in (self.listener, *self.sockets):
            with suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            sock.close()
        for thread in self.threads:
            thread.join(WAIT_LIMIT)


@contextmanager
def watched_connects(monkeypatch: pytest.MonkeyPatch, on_failure: Callable[[], None]) -> Generator[list[bool]]:
    # records every connection attempt (True when it failed) so the test can synchronise on the first failure
    import rowx.check.migrate as module  # ruff: ignore[import-outside-top-level]

    attempts: list[bool] = []
    original = module.connect

    async def spy(options: ConnectOptions):
        try:
            conn = await original(options)
        except Exception:
            attempts.append(True)
            on_failure()
            raise
        attempts.append(False)
        return conn

    monkeypatch.setattr(module, "connect", spy)
    yield attempts


def test_connect_timeout_retries_until_the_server_accepts(
    schema: str, migrations: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    params = conninfo_to_dict(schema)
    proxy = Proxy((str(params["host"]), int(str(params["port"]))))
    first_failure = threading.Event()

    # the listener starts accepting only after the CLI has made, and failed, a first attempt
    def open_listener() -> None:
        if first_failure.wait(WAIT_LIMIT):
            proxy.start()

    opener = threading.Thread(target=open_listener, daemon=True)
    opener.start()
    dsn = make_conninfo(schema, host="127.0.0.1", port=proxy.port, hostaddr="127.0.0.1")
    try:
        with watched_connects(monkeypatch, first_failure.set) as attempts:
            result = cli(capsys, dsn, migrations, "run", "--connect-timeout", "30")
    finally:
        first_failure.set()
        opener.join(WAIT_LIMIT)
        proxy.close()
    assert result == (0, golden("migrate_run.txt"), "")
    assert attempts[0] is True
    # the connection that succeeded is the one the command used: no probe, no second connection
    assert attempts.count(False) == 1
    assert attempts[-1] is False
    assert applied_versions(schema) == [1, 2]


def test_connect_timeout_gives_up_after_the_timeout(
    migrations: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "s3cr3t-" + secrets.token_hex(4)
    dsn = f"postgresql://someone:{secret}@127.0.0.1:{free_port()}/nope"
    with watched_connects(monkeypatch, lambda: None) as attempts:
        code, out, err = cli(capsys, dsn, migrations, "info", "--connect-timeout", "1")
    assert (code, out) == (2, "")
    assert err.startswith("error: could not reach the database")
    assert secret not in err
    # it retried, and the last failure is the error: no extra attempt to report it
    assert len(attempts) > 1
    assert all(attempts)


def test_a_zero_connect_timeout_tries_once(
    migrations: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    dsn = f"postgresql://u:p@127.0.0.1:{free_port()}/nope"
    with watched_connects(monkeypatch, lambda: None) as attempts:
        code, _, err = cli(capsys, dsn, migrations, "info", "--connect-timeout", "0")
    assert code == 2
    assert err.startswith("error: could not reach the database")
    assert attempts == [True]


# a listener that completes the TCP handshake but never answers, like a host that drops the startup packet
@contextmanager
def silent_listener() -> Generator[int]:
    with closing(socket.socket()) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        yield int(listener.getsockname()[1])


# runs `call` on a thread so a hang fails the test instead of blocking it
def finishes(call: Callable[[], object]) -> bool:
    thread = threading.Thread(target=call, daemon=True)
    thread.start()
    thread.join(WAIT_LIMIT)
    return not thread.is_alive()


def test_connect_timeout_bounds_an_attempt_to_a_server_that_never_answers(
    migrations: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    results: list[tuple[int, str, str]] = []
    with silent_listener() as port, watched_connects(monkeypatch, lambda: None) as attempts:
        dsn = f"postgresql://u:pw@127.0.0.1:{port}/nope"
        assert finishes(lambda: results.append(cli(capsys, dsn, migrations, "info", "--connect-timeout", "0.5")))
    assert results == [(2, "", "error: could not reach the database: connection timed out\n")]
    assert attempts == [True]


def test_a_wrong_password_is_not_retried(
    limited_dsn: tuple[str, str],
    migrations: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn, password = limited_dsn
    wrong = make_conninfo(dsn, password=f"{password}-wrong", host="127.0.0.1")
    with watched_connects(monkeypatch, lambda: None) as attempts:
        code, _, err = cli(capsys, wrong, migrations, "info", "--connect-timeout", "30")
    assert code == 2
    assert password not in err
    assert attempts == [True]
