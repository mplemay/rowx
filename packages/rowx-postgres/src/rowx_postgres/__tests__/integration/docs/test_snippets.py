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
# the pages whose snippets run the in-process engine, so they need the companion and not a server
EMBEDDED_DOCS: Final[frozenset[str]] = frozenset({"docs/guide/embedded.md"})
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


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


# the snippets that need what one embedded session cannot give, by file (relative to the repository root) and a line
# that only that snippet holds (an index would shift the marks whenever a snippet is added before it)
SERVER_SNIPPETS: Final[dict[tuple[str, str], pytest.MarkDecorator]] = {
    # one connection is enough for it, but a pool that opens before its first query races the release of the connection
    # it warmed: the acquire that finds nothing idle opens a second one and never looks at the idle list again, which one
    # session refuses (a pool bug, not an engine limit); about a third of runs time out
    ("README.md", "Pool(DSN, max_size=10)"): pytest.mark.server(needs="sessions"),
    ("docs/guide/executors.md", "min_size=1, max_size=4"): pytest.mark.server(needs="sessions"),
    ("docs/guide/executors.md", 'pool.transaction(isolation="serializable")'): pytest.mark.server(needs="sessions"),
    ("docs/guide/migrations.md", "0001_create_accounts.up.sql"): pytest.mark.server(needs="sessions"),
    # the plugin clones a template database
    ("docs/guide/testing.md", 'pytest_plugins = ["rowx.testing"]'): pytest.mark.server(needs="create-database"),
    # a client_encoding other than UTF8
    ("docs/guide/security.md", "client_encoding = 'SJIS'"): pytest.mark.server(divergence="encoding"),
}


def _bodies() -> list[tuple[Path, int, str]]:
    return [(path, i, m["body"]) for path in _doc_files() for i, m in enumerate(SNIPPET.finditer(path.read_text()))]


def _marks(path: Path, body: str) -> list[pytest.MarkDecorator]:
    name = _relative(path)
    marks = [mark for (file, fragment), mark in SERVER_SNIPPETS.items() if file == name and fragment in body]
    return [pytest.mark.embedded, *marks] if name in EMBEDDED_DOCS else marks


def _snippets() -> list[tuple[Path, int, str] | object]:
    return [
        pytest.param(path, i, body, marks=marks) if (marks := _marks(path, body)) else (path, i, body)
        for path, i, body in _bodies()
    ]


# a fragment that matches no snippet, or two, would leave a snippet unmarked or marked twice, silently
def test_every_server_snippet_key_matches_exactly_one_snippet() -> None:
    matches = {
        key: [i for path, i, body in _bodies() if _relative(path) == key[0] and key[1] in body]
        for key in SERVER_SNIPPETS
    }
    assert {key: len(found) for key, found in matches.items()} == dict.fromkeys(SERVER_SNIPPETS, 1)


def test_readme_and_guide_are_discovered() -> None:
    discovered = set(_doc_files())
    assert ROOT / "README.md" in discovered
    assert ROOT / "docs" / "guide.md" in discovered


def test_every_embedded_doc_is_discovered_and_has_snippets() -> None:
    snippeted = {_relative(path) for path, _, _ in _bodies()}
    assert snippeted >= EMBEDDED_DOCS


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
    assert "from rowx.postgres import Pool" in readme
    assert "from rowx.postgres import connect" in readme
    assert "rowx[postgres]" in readme
    assert "from rowx_postgres import" not in readme
    index = (ROOT / "docs" / "index.md").read_text()
    assert "from rowx.postgres import connect" in index
    assert "from rowx_postgres import" not in index
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
    assert "tstring()" in why_text
    assert "not a t-string" not in why_text
    assert "The checker never imports" in why_text

    assert "why.md" in zensical


def test_parametrization_runs_every_fence() -> None:
    # The decorator sets pytestmark at import. ty does not treat that attribute as declared.
    raw = vars(test_snippet_runs).get("pytestmark")
    assert raw is not None, "test_snippet_runs is missing pytestmark"
    marks = raw if isinstance(raw, list) else [raw]
    parametrize = next((mark for mark in marks if mark.name == "parametrize"), None)
    assert parametrize is not None, "test_snippet_runs is missing the parametrize mark"
    assert list(parametrize.args[1]) == _snippets()
