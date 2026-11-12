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
USE_ROWX_SKILL: Final[Path] = ROOT / ".agents" / "skills" / "use-rowx" / "SKILL.md"
USE_ROWX_SKILL_URL: Final[str] = "https://github.com/mplemay/rowx/blob/main/.agents/skills/use-rowx/SKILL.md"
FENCED_BLOCK: Final[Pattern[str]] = re_compile(r"^```.*?^```$", MULTILINE | DOTALL)
INLINE_CODE: Final[Pattern[str]] = re_compile(r"`[^`]*`")
INLINE_LINK: Final[Pattern[str]] = re_compile(r"\[[^]]*\]\(([^)]*)\)")
REFERENCE_DEFINITION: Final[Pattern[str]] = re_compile(r"^\[[^]]+\]:[ \t]+(\S+)", MULTILINE)
EXTERNAL_LINK_PREFIXES: Final[tuple[str, ...]] = ("http://", "https://", "mailto:")


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
    assert "from rowx.postgres import Pool" in readme
    assert "from rowx.postgres import connect" in readme
    assert "rowx[postgres]" in readme
    assert "from rowx_postgres import" not in readme
    index = (ROOT / "docs" / "index.md").read_text()
    assert "from rowx import query" in index
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


def _link_destinations(text: str) -> list[str]:
    # Fenced blocks and inline code are not markdown links. `query[T](t"...")` is code, not a link.
    prose = INLINE_CODE.sub("", FENCED_BLOCK.sub("", text))
    inline = [match.group(1) for match in INLINE_LINK.finditer(prose)]
    defined = [match.group(1) for match in REFERENCE_DEFINITION.finditer(prose)]
    return [*inline, *defined]


def _relative_target(destination: str) -> str | None:
    target = destination.strip()
    if len(target) >= 2 and target[0] == "<" and target[-1] == ">":
        target = target[1:-1].strip()
    target = target.split(maxsplit=1)[0]
    if target.startswith(EXTERNAL_LINK_PREFIXES):
        return None
    path = target.split("#", 1)[0]
    if not path:
        return None
    return path


def test_use_rowx_skill_is_discovered() -> None:
    assert USE_ROWX_SKILL in set(_doc_files())


def test_use_rowx_skill_is_linked() -> None:
    readme = (ROOT / "README.md").read_text()
    index = (ROOT / "docs" / "index.md").read_text()
    testing = (ROOT / "docs" / "contributing" / "testing.md").read_text()
    assert ".agents/skills/use-rowx/SKILL.md" in readme
    assert USE_ROWX_SKILL_URL in index
    assert USE_ROWX_SKILL_URL in testing


def test_use_rowx_skill_links_resolve() -> None:
    sources = sorted(USE_ROWX_SKILL.parent.rglob("*.md"))
    skill_root = USE_ROWX_SKILL.parent.resolve()
    assert sources
    for source in sources:
        for destination in _link_destinations(source.read_text()):
            if (relative := _relative_target(destination)) is None:
                continue
            resolved = (source.parent / relative).resolve()
            assert resolved.is_relative_to(skill_root), f"{source.relative_to(ROOT)} -> {destination}"
            assert resolved.is_file(), f"{source.relative_to(ROOT)} -> {destination}"


def _python_fence_after(text: str, label: str) -> str:
    start = text.index(f"{label}:")
    fence = text.index("```python\n", start)
    body_start = fence + len("```python\n")
    return text[body_start : text.index("\n```", body_start)]


def test_skill_import_rule_does_not_export_connect() -> None:
    text = (USE_ROWX_SKILL.parent / "rules" / "imports.md").read_text()
    assert "`connect` imported from `rowx` or `rowx_core`" not in text
    assert "`connect` is not exported by `rowx.core`, `rowx`, or `rowx_core`" in text
    assert "from rowx_postgres import" not in _python_fence_after(text, "Incorrect")


def test_skill_reset_rule_names_the_role() -> None:
    text = (USE_ROWX_SKILL.parent / "rules" / "reset.md").read_text()
    assert "neither the role nor session authorization" in text


def test_skill_migration_samples_match_the_commands() -> None:
    rules = (USE_ROWX_SKILL.parent / "rules" / "migrations.md").read_text()
    add, reset = rules.split("## Validate before reset drops", maxsplit=1)
    assert "rowx migrate add" in add
    assert "migrator.run" not in add
    assert "rowx database reset" in reset
    assert "rowx database drop" in reset
    reference = (USE_ROWX_SKILL.parent / "references" / "migrations.md").read_text()
    assert "rowx migrate add" in reference
    assert "rowx database reset" in reference
    assert "migrator.run" not in reference


def test_skill_unchecked_section_calls_query_unchecked() -> None:
    text = (USE_ROWX_SKILL.parent / "rules" / "diagnostics.md").read_text()
    heading = "## `query_unchecked` SQL is not described"
    start = text.index(heading)
    next_heading = text.find("\n## ", start + 1)
    section = text[start:] if next_heading < 0 else text[start:next_heading]
    incorrect = _python_fence_after(section, "Incorrect")
    correct = _python_fence_after(section, "Correct")
    assert "query_unchecked[str]" in incorrect
    assert "query[str]" in correct
    assert "query_unchecked" not in correct
    assert "SQL is not described" in section
    assert "dict" not in section
    assert "query[str](sql)" in text[:start]


def test_skill_any_list_is_one_parameter() -> None:
    text = (USE_ROWX_SKILL.parent / "rules" / "parameters.md").read_text()
    start = text.index("## Lists use ANY")
    section = text[start : text.index("\n## ", start + 1)]
    assert "one parameter" in section
    incorrect = _python_fence_after(section, "Incorrect")
    assert any('f"' in line for line in incorrect.splitlines())
    call = incorrect[incorrect.index("query[") :].split(")", 1)[0]
    assert 't"' not in call
    assert "ANY(" in _python_fence_after(section, "Correct")


def test_skill_drift_gate_states_its_conditions() -> None:
    text = (USE_ROWX_SKILL.parent / "references" / "migrations.md").read_text()
    assert "--no-drift-check" in text
    assert "--scratch" in text
    assert "would be described" in text


def test_skill_fetch_has_no_io_until_iteration() -> None:
    text = (USE_ROWX_SKILL.parent / "references" / "queries.md").read_text()
    assert "no IO until iteration" in text


def test_skill_testing_reference_allows_asyncio_tests() -> None:
    text = (USE_ROWX_SKILL.parent / "references" / "testing.md").read_text()
    assert "asyncio.run" in text
    assert "Trio" in text


def test_skill_integrations_cover_sqlalchemy_and_fastapi() -> None:
    skill = USE_ROWX_SKILL.read_text()
    sqlalchemy = (USE_ROWX_SKILL.parent / "references" / "sqlalchemy.md").read_text()
    fastapi = (USE_ROWX_SKILL.parent / "references" / "fastapi.md").read_text()
    assert "references/sqlalchemy.md" in skill
    assert "references/fastapi.md" in skill
    assert "unit of work" in skill
    assert "identity map" in skill
    for text in (skill, sqlalchemy):
        assert "tstring()" in text
        assert "scalar_one" in text
        assert "relationship()" in text
        assert "rowx migrate" in text
    assert "pydantic" in sqlalchemy
    assert "Pool(DSN)" in sqlalchemy
    assert "lifespan" in fastapi
    assert "Depends" in fastapi
    assert "await pool.close()" in fastapi
    assert 'scope="function"' in fastapi
    assert "RowNotFound" in fastapi
    assert "pool.acquire()" in fastapi
    assert "pool.transaction()" in fastapi


def test_parametrization_runs_every_fence() -> None:
    # The decorator sets pytestmark at import. ty does not treat that attribute as declared.
    raw = vars(test_snippet_runs).get("pytestmark")
    assert raw is not None, "test_snippet_runs is missing pytestmark"
    marks = raw if isinstance(raw, list) else [raw]
    parametrize = next((mark for mark in marks if mark.name == "parametrize"), None)
    assert parametrize is not None, "test_snippet_runs is missing the parametrize mark"
    assert list(parametrize.args[1]) == _snippets()
