import tempfile
from os import environ
from pathlib import Path
from re import DOTALL, MULTILINE, Pattern, compile as re_compile
from runpy import run_path
from typing import TYPE_CHECKING, Final

import psycopg
import pytest
from psycopg import sql

from rowx.testing import TEMPLATE_PREFIX, project_prefix

if TYPE_CHECKING:
    from collections.abc import Iterator

ROOT: Final[Path] = next(parent for parent in Path(__file__).resolve().parents if (parent / "Cargo.lock").is_file())
# `.agents/skills/` may be absent; the pattern stays so a later skill page is picked up.
DOC_GLOBS: Final[tuple[str, ...]] = ("README.md", "docs/**/*.md", ".agents/skills/**/*.md")
SNIPPET: Final[Pattern[str]] = re_compile(r"^```python test\n(?P<body>.*?)^```$", MULTILINE | DOTALL)


# the testing guide snippet runs the plugin in a temporary project, whose template (cached for the next run of a real
# project) would otherwise outlive the test. Only that project's templates (its `<p>`, from the directories the snippet
# made) are dropped, never one another session on the server is using
@pytest.fixture
def drop_snippet_templates(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    made: list[str] = []
    mkdtemp = tempfile.mkdtemp

    # `TemporaryDirectory` passes its arguments by position
    def recording(*args: str | None) -> str:
        made.append(path := mkdtemp(*args))
        return path

    monkeypatch.setattr(tempfile, "mkdtemp", recording)
    yield
    monkeypatch.undo()
    if (dsn := environ.get("ROWX_TEST_DSN")) is None or not made:
        return
    prefixes = [f"{TEMPLATE_PREFIX}{project_prefix(Path(path) / 'migrations')}_" for path in made]
    with psycopg.connect(dsn, autocommit=True) as conn:
        rows = conn.execute("SELECT datname FROM pg_database WHERE datname::text ^@ ANY(%s::text[])", (prefixes,))
        for (name,) in rows.fetchall():
            conn.execute(sql.SQL("ALTER DATABASE {} WITH IS_TEMPLATE false").format(sql.Identifier(name)))
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def _doc_files() -> tuple[Path, ...]:
    found = {path for pattern in DOC_GLOBS for path in ROOT.glob(pattern) if path.is_file()}
    return tuple(sorted(found))


def _snippets() -> list[tuple[Path, int, str]]:
    return [(path, i, m["body"]) for path in _doc_files() for i, m in enumerate(SNIPPET.finditer(path.read_text()))]


def test_readme_and_guide_are_discovered() -> None:
    discovered = set(_doc_files())
    assert ROOT / "README.md" in discovered
    assert ROOT / "docs" / "guide.md" in discovered


@pytest.mark.db
@pytest.mark.usefixtures("drop_snippet_templates")
@pytest.mark.parametrize(("path", "index", "body"), _snippets(), ids=lambda v: v.name if isinstance(v, Path) else None)
def test_snippet_runs(path: Path, index: int, body: str, schema: str, tmp_path: Path) -> None:
    script = tmp_path / f"snippet_{path.stem}_{index}.py"
    script.write_text(body)
    run_path(str(script), init_globals={"DSN": schema})


def test_published_readmes_use_canonical_imports() -> None:
    readme = (ROOT / "README.md").read_text()
    core_readme = (ROOT / "packages" / "rowx-core" / "README.md").read_text()
    postgres_readme = (ROOT / "packages" / "rowx-postgres" / "README.md").read_text()
    why = ROOT / "docs" / "why.md"
    zensical = (ROOT / "zensical.toml").read_text()
    assert why.is_file()
    why_text = why.read_text()

    assert "from rowx import query" in readme
    assert "from rowx_postgres import" in readme
    assert "rowx[postgres]" in readme
    assert "from rowx.postgres import" not in readme
    assert "docs/why.md" in readme
    assert "docs/changelog.md" in readme
    assert "prepares each checked" in readme
    assert "query_unchecked" in readme
    assert "no diagnostics" in readme

    assert "from rowx_core import query" in core_readme
    assert "no runtime dependencies" in core_readme
    assert "rowx[postgres]" in core_readme
    assert "uv add rowx-core rowx-postgres" in core_readme

    assert "from rowx_postgres import Pool, connect" in postgres_readme
    assert "depends only on" in postgres_readme
    assert "rowx-core" in postgres_readme
    assert "uv add rowx-core rowx-postgres" in postgres_readme
    assert "rowx[postgres]" in postgres_readme

    assert "SQLx" in why_text
    assert "asyncpg" in why_text
    assert "psycopg" in why_text
    assert "SQLAlchemy" in why_text
    assert "sqlc" in why_text
    assert "query_unchecked" in why_text
    assert "query_as!" in why_text
    assert "do not describe mapped objects" not in why_text
    assert "The FAQ is about" not in why_text

    assert "why.md" in zensical


def test_parametrization_runs_every_fence() -> None:
    # The decorator sets pytestmark at import. ty does not treat that attribute as declared.
    raw = vars(test_snippet_runs).get("pytestmark")
    assert raw is not None, "test_snippet_runs is missing pytestmark"
    marks = raw if isinstance(raw, list) else [raw]
    parametrize = next((mark for mark in marks if mark.name == "parametrize"), None)
    assert parametrize is not None, "test_snippet_runs is missing the parametrize mark"
    assert list(parametrize.args[1]) == _snippets()
