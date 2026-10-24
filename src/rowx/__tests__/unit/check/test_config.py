from pathlib import Path

import pytest

from rowx.check.config import (
    DEFAULT_EXCLUDE,
    DEFAULT_STATEMENT_TIMEOUT_MS,
    ConfigError,
    find_pyproject,
    load_config,
    parse_duration,
)


def write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("text", "expected"),
    [("250ms", 250), ("10s", 10_000), ("2min", 120_000), (" 1s ", 1000)],
)
def test_parse_duration(text: str, expected: int) -> None:
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "10", "s", "1.5s", "10 s", "-1s", "10m", "0s", "1h"])
def test_parse_duration_rejects(text: str) -> None:
    with pytest.raises(ConfigError, match="invalid duration"):
        parse_duration(text)


def test_searches_upward_for_pyproject(tmp_path: Path) -> None:
    write(tmp_path / "pyproject.toml", '[tool.rowx]\ninclude = ["src"]\n')
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    config = load_config(nested)
    assert config.root == tmp_path.resolve()
    assert config.include == ("src",)
    assert find_pyproject(nested) == tmp_path.resolve() / "pyproject.toml"


def test_nearest_pyproject_wins(tmp_path: Path) -> None:
    write(tmp_path / "pyproject.toml", '[tool.rowx]\ninclude = ["outer"]\n')
    inner = tmp_path / "pkg"
    inner.mkdir()
    write(inner / "pyproject.toml", "[project]\nname = 'x'\n")
    config = load_config(inner)
    assert config.root == inner.resolve()
    assert config.include == (".",)


def test_missing_table_means_defaults(tmp_path: Path) -> None:
    write(tmp_path / "pyproject.toml", "[project]\nname = 'x'\n")
    config = load_config(tmp_path)
    assert config.include == (".",)
    assert config.exclude == ()
    assert config.database_url_env == "DATABASE_URL"
    assert config.statement_timeout_ms == DEFAULT_STATEMENT_TIMEOUT_MS
    assert config.all_excludes == DEFAULT_EXCLUDE


def test_no_pyproject_means_defaults(tmp_path: Path) -> None:
    # tmp_path normally has no pyproject.toml above it
    if find_pyproject(tmp_path) is not None:
        return
    assert load_config(tmp_path).root == tmp_path.resolve()


def test_full_table(tmp_path: Path) -> None:
    write(
        tmp_path / "pyproject.toml",
        '[tool.rowx]\ninclude = ["src"]\nexclude = ["**/migrations/**"]\n'
        'database_url_env = "PG_URL"\nstatement_timeout = "2min"\n',
    )
    config = load_config(tmp_path)
    assert config.exclude == ("**/migrations/**",)
    assert config.all_excludes == (*DEFAULT_EXCLUDE, "**/migrations/**")
    assert config.database_url_env == "PG_URL"
    assert config.statement_timeout_ms == 120_000


def test_unknown_key_suggests(tmp_path: Path) -> None:
    write(tmp_path / "pyproject.toml", '[tool.rowx]\nstatment_timeout = "1s"\n')
    with pytest.raises(ConfigError, match=r"unknown key 'statment_timeout'.*did you mean 'statement_timeout'\?"):
        load_config(tmp_path)


def test_unknown_key_without_suggestion(tmp_path: Path) -> None:
    write(tmp_path / "pyproject.toml", "[tool.rowx]\nzzzzzz = 1\n")
    with pytest.raises(ConfigError, match="unknown key 'zzzzzz'") as info:
        load_config(tmp_path)
    assert "did you mean" not in str(info.value)


@pytest.mark.parametrize(
    "body",
    [
        "include = 'src'",
        "exclude = [1]",
        "database_url_env = 3",
        "statement_timeout = 10",
        'statement_timeout = "10"',
    ],
)
def test_invalid_values(tmp_path: Path, body: str) -> None:
    write(tmp_path / "pyproject.toml", f"[tool.rowx]\n{body}\n")
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_invalid_toml(tmp_path: Path) -> None:
    write(tmp_path / "pyproject.toml", "[tool.rowx\n")
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_explicit_config_file(tmp_path: Path) -> None:
    write(tmp_path / "pyproject.toml", '[tool.rowx]\ninclude = ["a"]\n')
    other = tmp_path / "other"
    other.mkdir()
    write(other / "custom.toml", '[tool.rowx]\ninclude = ["b"]\n')
    config = load_config(tmp_path, Path("other/custom.toml"))
    assert config.include == ("b",)
    assert config.root == other.resolve()


def test_missing_explicit_config_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="does not exist"):
        load_config(tmp_path, Path("nope.toml"))
