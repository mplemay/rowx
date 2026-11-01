from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from typing import TYPE_CHECKING, Final

import pytest

from rowx.check.cli import main
from rowx.check.migrate import format_duration

if TYPE_CHECKING:
    from pathlib import Path


def test_check_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["check", "--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == version("rowx")


TIMESTAMP: Final[datetime] = datetime(2026, 10, 29, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("rowx.check.migrate.utc_now", lambda: TIMESTAMP)
    return tmp_path


def _add(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(["migrate", "add", *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _names(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir())


def _seed(directory: Path, *names: str) -> None:
    directory.mkdir(exist_ok=True)
    for name in names:
        (directory / name).write_text("SELECT 1;\n", encoding="utf-8")


def test_add_creates_the_directory_with_sqlx_placeholder_content(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = _add(capsys, "create_users")
    assert (code, err) == (0, "")
    assert out == "Created migrations/20261029120000_create_users.sql\n"
    created = project / "migrations" / "20261029120000_create_users.sql"
    assert created.read_text(encoding="utf-8") == "-- Add migration script here\n"


def test_add_replaces_spaces_in_the_description(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _add(capsys, "create the users")
    assert _names(project / "migrations") == ["20261029120000_create_the_users.sql"]


def test_add_timestamp_is_utc_to_the_second(
    project: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = datetime(2027, 1, 2, 3, 4, 5, tzinfo=UTC)
    monkeypatch.setattr("rowx.check.migrate.utc_now", lambda: other)
    _add(capsys, "x")
    assert _names(project / "migrations") == ["20270102030405_x.sql"]


def test_add_with_no_migrations_infers_a_timestamp(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _add(capsys, "first")
    assert _names(project / "migrations") == ["20261029120000_first.sql"]


@pytest.mark.parametrize(
    ("existing", "expected"),
    [
        (["0001_a.sql", "0002_b.sql"], "0003_next.sql"),
        (["5_a.sql", "6_b.sql"], "0007_next.sql"),
        (["0001_a.sql"], "0002_next.sql"),
        (["0001_a.sql", "0003_b.sql"], "20261029120000_next.sql"),
        (["20260101000000_a.sql"], "20261029120000_next.sql"),
        (["20260101000000_a.sql", "20260102000000_b.sql"], "20261029120000_next.sql"),
        (["7_a.sql"], "20261029120000_next.sql"),
    ],
)
def test_add_infers_versioning_by_sqlxs_rule(
    project: Path,
    capsys: pytest.CaptureFixture[str],
    existing: list[str],
    expected: str,
) -> None:
    _seed(project / "migrations", *existing)
    assert _add(capsys, "next")[0] == 0
    assert expected in _names(project / "migrations")


def test_add_reversible_pairs_use_the_up_version_for_the_rule(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(project / "migrations", "1_a.up.sql", "1_a.down.sql", "2_b.up.sql", "2_b.down.sql")
    _add(capsys, "next")
    assert "0003_next.up.sql" in _names(project / "migrations")


def test_add_flags_override_inference(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed(project / "migrations", "0001_a.sql", "0002_b.sql")
    _add(capsys, "--timestamp", "stamped")
    _seed(project / "other", "20260101000000_a.sql", "20260102000000_b.sql")
    _add(capsys, "--sequential", "--source", "other", "counted")
    assert "20261029120000_stamped.sql" in _names(project / "migrations")
    assert "20260102000001_counted.sql" in _names(project / "other")


def test_add_reversible_writes_a_pair(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = _add(capsys, "--reversible", "create users")
    assert code == 0
    assert out == (
        "Created migrations/20261029120000_create_users.up.sql\nCreated migrations/20261029120000_create_users.down.sql\n"
    )
    directory = project / "migrations"
    assert (directory / "20261029120000_create_users.up.sql").read_text(encoding="utf-8") == (
        "-- Add up migration script here\n"
    )
    assert (directory / "20261029120000_create_users.down.sql").read_text(encoding="utf-8") == (
        "-- Add down migration script here\n"
    )


def test_add_kind_is_inferred_from_the_latest_migration(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed(project / "migrations", "0001_a.up.sql", "0001_a.down.sql")
    _add(capsys, "after reversible")
    _seed(project / "simple", "0001_a.up.sql", "0001_a.down.sql", "0002_b.sql")
    _add(capsys, "--source", "simple", "after simple")
    assert _names(project / "migrations")[-2:] == ["0002_after_reversible.down.sql", "0002_after_reversible.up.sql"]
    assert "0003_after_simple.sql" in _names(project / "simple")


def test_add_simple_flag_beats_a_reversible_history(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed(project / "migrations", "0001_a.up.sql", "0001_a.down.sql")
    _add(capsys, "--simple", "plain")
    assert "0002_plain.sql" in _names(project / "migrations")


def test_add_refuses_to_overwrite(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _add(capsys, "again")[0] == 0
    (project / "migrations" / "20261029120000_again.sql").write_text("CREATE TABLE keep (id int);\n", encoding="utf-8")
    code, out, err = _add(capsys, "again")
    assert (code, out) == (2, "")
    assert "refusing to overwrite" in err
    assert (project / "migrations" / "20261029120000_again.sql").read_text(
        encoding="utf-8"
    ) == "CREATE TABLE keep (id int);\n"


def test_add_refuses_a_version_that_exists_before_writing_a_pair(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(project / "migrations", "20261029120000_a.sql")
    code, _, err = _add(capsys, "--reversible", "b")
    assert code == 2
    assert "refusing to overwrite" in err
    assert _names(project / "migrations") == ["20261029120000_a.sql"]


def test_add_reads_dir_versioning_and_kind_from_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.rowx.migrate]\ndir = "db/sql"\nversioning = "sequential"\nkind = "reversible"\n',
        encoding="utf-8",
    )
    nested = tmp_path / "src" / "pkg"
    nested.mkdir(parents=True)
    # the configured dir is relative to pyproject.toml, not to the current directory
    monkeypatch.chdir(nested)
    assert _add(capsys, "first")[0] == 0
    assert _names(tmp_path / "db" / "sql") == ["0001_first.down.sql", "0001_first.up.sql"]
    # a flag beats the config
    _add(capsys, "--simple", "--timestamp", "second")
    assert any(name.endswith("_second.sql") and name.startswith("20") for name in _names(tmp_path / "db" / "sql"))


def test_add_source_is_relative_to_the_current_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.rowx.migrate]\ndir = "ignored"\n', encoding="utf-8")
    nested = tmp_path / "sub"
    nested.mkdir()
    monkeypatch.chdir(nested)
    assert _add(capsys, "--source", "here", "x")[0] == 0
    assert (nested / "here").is_dir()
    assert not (tmp_path / "ignored").exists()


def test_add_without_a_pyproject_defaults_to_migrations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    assert _add(capsys, "x")[0] == 0
    assert (tmp_path / "migrations").is_dir()


@pytest.mark.parametrize("description", ["", "   ", "a/b", "a\\b", "a\0b"])
def test_add_rejects_a_bad_description(project: Path, capsys: pytest.CaptureFixture[str], description: str) -> None:
    code, out, err = _add(capsys, description)
    assert (code, out) == (2, "")
    assert err.startswith("error: the description")
    assert not (project / "migrations").exists()


@pytest.mark.parametrize("description", ["cleanup.up", "cleanup.down"])
def test_add_rejects_a_simple_description_that_reads_back_as_half_of_a_pair(
    project: Path, capsys: pytest.CaptureFixture[str], description: str
) -> None:
    code, out, err = _add(capsys, "--simple", "--sequential", description)
    assert (code, out) == (2, "")
    assert err == "error: a simple migration's description must not end in .up or .down\n"
    assert not (project / "migrations").exists()


def test_add_a_reversible_description_may_end_in_up(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _add(capsys, "--reversible", "--sequential", "cleanup.up")[0] == 0
    assert _names(project / "migrations") == ["0001_cleanup.up.down.sql", "0001_cleanup.up.up.sql"]
    # the directory still loads, so the next add infers from it
    assert _add(capsys, "next")[0] == 0
    assert "0002_next.up.sql" in _names(project / "migrations")


def test_add_reports_a_directory_it_cannot_create(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (project / "migrations").write_text("", encoding="utf-8")
    code, out, err = _add(capsys, "x")
    assert (code, out) == (2, "")
    assert err.startswith("error: ")


def test_add_reports_a_bad_existing_directory(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed(project / "migrations", "nonsense.sql")
    code, _, err = _add(capsys, "x")
    assert code == 2
    assert "nonsense.sql" in err


def test_add_reports_a_bad_config(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (project / "pyproject.toml").write_text('[tool.rowx.migrate]\nkind = "both"\n', encoding="utf-8")
    code, _, err = _add(capsys, "x")
    assert code == 2
    assert "invalid kind 'both'" in err


@pytest.mark.parametrize(
    "argv",
    [["--reversible", "--simple"], ["--timestamp", "--sequential"]],
)
def test_add_conflicting_flags_are_a_usage_error(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["migrate", "add", *argv, "x"])
    assert exc.value.code == 2


@pytest.mark.parametrize("command", ["run", "revert", "info"])
def test_database_commands_need_a_url(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    code = main(["migrate", command])
    captured = capsys.readouterr()
    assert code == 2
    assert not captured.out
    assert "no database URL; pass --database-url or set $DATABASE_URL" in captured.err
    assert not (project / "migrations").exists()


def test_database_url_env_comes_from_the_config(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (project / "pyproject.toml").write_text('[tool.rowx]\ndatabase_url_env = "MY_URL"\n', encoding="utf-8")
    monkeypatch.delenv("MY_URL", raising=False)
    assert main(["migrate", "info"]) == 2
    assert "$MY_URL" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["run", "revert", "info"])
def test_an_unreachable_database_exits_two_without_the_password(
    project: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    url = "postgresql://nobody:hunter2secret@127.0.0.1:1/nope?connect_timeout=2"
    _seed(project / "migrations", "1_a.sql")
    code = main(["migrate", command, "--database-url", url])
    captured = capsys.readouterr()
    assert code == 2
    assert not captured.out
    assert captured.err.startswith("error: ")
    assert "hunter2secret" not in captured.err
    assert url not in captured.err


def test_a_bad_migration_directory_exits_two(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed(project / "migrations", "nonsense.sql")
    code = main(["migrate", "info", "--database-url", "postgresql://u:p@127.0.0.1:1/d"])
    assert code == 2
    assert "nonsense.sql" in capsys.readouterr().err


def test_a_missing_config_file_exits_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["migrate", "info", "--config", "nope.toml"]) == 2
    assert "config file does not exist" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "n/a"),
        (timedelta(microseconds=250), "250.0us"),
        (timedelta(microseconds=12300), "12.3ms"),
        (timedelta(milliseconds=1500), "1.5s"),
    ],
)
def test_format_duration(value: timedelta | None, expected: str) -> None:
    assert format_duration(value) == expected
