import tomllib
from dataclasses import dataclass, field
from difflib import get_close_matches
from re import fullmatch
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from pathlib import Path


PYPROJECT: Final[str] = "pyproject.toml"
DEFAULT_INCLUDE: Final[tuple[str, ...]] = (".",)
DEFAULT_DATABASE_URL_ENV: Final[str] = "DATABASE_URL"
DEFAULT_STATEMENT_TIMEOUT_MS: Final[int] = 10_000
DEFAULT_EXCLUDE: Final[tuple[str, ...]] = (
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "site-packages",
    "build",
    "dist",
)
KEYS: Final[tuple[str, ...]] = ("include", "exclude", "database_url_env", "statement_timeout")
DURATION_UNITS_MS: Final[dict[str, int]] = {"ms": 1, "s": 1000, "min": 60_000}


class ConfigError(Exception):
    pass


@dataclass(slots=True, kw_only=True, frozen=True)
class Config:
    root: Path
    include: tuple[str, ...] = DEFAULT_INCLUDE
    # user excludes only; the default excludes are always applied on top
    exclude: tuple[str, ...] = ()
    database_url_env: str = DEFAULT_DATABASE_URL_ENV
    statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS
    source: Path | None = field(default=None)

    @property
    def all_excludes(self) -> tuple[str, ...]:
        return (*DEFAULT_EXCLUDE, *self.exclude)


def parse_duration(value: str) -> int:
    if (match := fullmatch(r"(\d+)(ms|s|min)", value.strip())) is None:
        msg = f'invalid duration {value!r}; expected an integer and a unit (ms, s or min), such as "10s"'
        raise ConfigError(msg)
    milliseconds = int(match[1]) * DURATION_UNITS_MS[match[2]]
    if milliseconds <= 0:
        msg = f"invalid duration {value!r}; it must be greater than zero"
        raise ConfigError(msg)
    return milliseconds


def find_pyproject(start: Path) -> Path | None:
    start = start.resolve()
    for directory in (start, *start.parents):
        if (candidate := directory / PYPROJECT).is_file():
            return candidate
    return None


def load_config(cwd: Path, config_file: Path | None = None) -> Config:
    if config_file is not None:
        path = config_file if config_file.is_absolute() else cwd / config_file
        if not path.is_file():
            msg = f"config file does not exist: {config_file}"
            raise ConfigError(msg)
    elif (path := find_pyproject(cwd)) is None:
        return Config(root=cwd.resolve())
    return _load(path.resolve())


def _load(path: Path) -> Config:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        msg = f"{path}: {error}"
        raise ConfigError(msg) from None
    table = data.get("tool", {}).get("rowx", {})
    if not isinstance(table, dict):
        msg = f"{path}: [tool.rowx] must be a table"
        raise ConfigError(msg)
    for key in table:
        if key not in KEYS:
            hint = f" (did you mean {close[0]!r}?)" if (close := get_close_matches(key, KEYS, n=1)) else ""
            msg = f"{path}: unknown key {key!r} in [tool.rowx]{hint}; expected one of: {', '.join(KEYS)}"
            raise ConfigError(msg)
    return Config(
        root=path.parent,
        include=_strings(path, table, "include", DEFAULT_INCLUDE),
        exclude=_strings(path, table, "exclude", ()),
        database_url_env=_string(path, table, "database_url_env", DEFAULT_DATABASE_URL_ENV),
        statement_timeout_ms=(
            _duration(path, table["statement_timeout"])
            if "statement_timeout" in table
            else DEFAULT_STATEMENT_TIMEOUT_MS
        ),
        source=path,
    )


def _duration(path: Path, value: object) -> int:
    if not isinstance(value, str):
        msg = f'{path}: statement_timeout must be a string such as "10s"'
        raise ConfigError(msg)
    try:
        return parse_duration(value)
    except ConfigError as error:
        msg = f"{path}: statement_timeout: {error}"
        raise ConfigError(msg) from None


def _string(path: Path, table: dict[str, object], key: str, default: str) -> str:
    value = table.get(key, default)
    if not isinstance(value, str) or not value:
        msg = f"{path}: {key} must be a non-empty string"
        raise ConfigError(msg)
    return value


def _strings(path: Path, table: dict[str, object], key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    if key not in table:
        return default
    value = table[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        msg = f"{path}: {key} must be a list of strings"
        raise ConfigError(msg)
    return tuple(str(item) for item in value)
