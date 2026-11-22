import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import pytest
import pytest_asyncio
from rowx_sqlite import Connection, ConnectOptions, Migrator, connect

from rowx.check.cli import main

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


@pytest_asyncio.fixture
async def conn(tmp_path: Path) -> AsyncIterator[Connection]:
    connection = await connect(ConnectOptions(path=str(tmp_path / "test.db"), mode="rwc"))
    try:
        yield connection
    finally:
        await connection.close()


MIGRATION: Final[str] = """\
CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT, age INTEGER NOT NULL);
CREATE TABLE posts (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users (id), title TEXT NOT NULL);
CREATE VIEW user_posts AS SELECT users.id AS user_id, posts.title AS title FROM users LEFT JOIN posts ON posts.user_id = users.id;
CREATE TABLE loose (a, b);
"""


@dataclass(slots=True)
class CheckProject:
    root: Path
    capsys: pytest.CaptureFixture[str]

    @property
    def url(self) -> str:
        return f"sqlite:///{self.root / 'app.db'}"

    def write(self, source: str, name: str = "queries.py") -> Path:
        path = self.root / name
        path.write_text(source, encoding="utf-8")
        return path

    def check(self, *args: str, url: str | None = None) -> tuple[int, str, str]:
        code = main(["check", "--database-url", url or self.url, *args])
        captured = self.capsys.readouterr()
        return code, captured.out, captured.err


async def migrate(database: Path, migrations: Path) -> None:
    async with await connect(ConnectOptions(path=str(database), mode="rwc")) as connection:
        await Migrator(migrations).run(connection)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> CheckProject:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[tool.rowx.migrate]\n", encoding="utf-8")
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "1_init.sql").write_text(MIGRATION, encoding="utf-8")
    built = CheckProject(tmp_path, capsys)
    asyncio.run(migrate(built.root / "app.db", migrations))
    capsys.readouterr()
    return built
