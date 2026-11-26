from pathlib import Path
from re import DOTALL, MULTILINE, Pattern, compile as re_compile
from runpy import run_path
from typing import Final

import pytest

ROOT: Final[Path] = next(parent for parent in Path(__file__).resolve().parents if (parent / "Cargo.lock").is_file())
# `.agents/skills/` may be absent; the pattern stays so a later skill page is picked up.
DOC_GLOBS: Final[tuple[str, ...]] = ("README.md", "docs/**/*.md", ".agents/skills/**/*.md")
# the pages whose snippets run against SQLite. The PostgreSQL runner (`rowx_postgres` integration/docs/test_snippets.py)
# skips every snippet that references SQLite, so a snippet is in exactly one lane
SQLITE_DOCS: Final[frozenset[str]] = frozenset({
    "docs/guide/sqlite.md",
    ".agents/skills/use-rowx/references/sqlite.md",
})
SQLITE_WORDS: Final[tuple[str, ...]] = ("rowx.sqlite", "rowx_sqlite", "sqlite:")
SNIPPET: Final[Pattern[str]] = re_compile(r"^```python test\n(?P<body>.*?)^```$", MULTILINE | DOTALL)


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _pages() -> list[tuple[Path, int, str]]:
    found = sorted({path for pattern in DOC_GLOBS for path in ROOT.glob(pattern) if path.is_file()})
    return [(path, i, match["body"]) for path in found for i, match in enumerate(SNIPPET.finditer(path.read_text()))]


def _bodies() -> list[tuple[Path, int, str]]:
    return [(path, i, body) for path, i, body in _pages() if _relative(path) in SQLITE_DOCS]


def test_every_sqlite_doc_exists_and_has_snippets() -> None:
    snippeted = {_relative(path) for path, _, _ in _bodies()}
    assert snippeted == SQLITE_DOCS


# the PostgreSQL runner skips a snippet that references SQLite, so this runner must run exactly those: the pages holding
# one are the SQLITE_DOCS, and every snippet on those pages references SQLite
def test_the_sqlite_lane_is_the_snippets_that_reference_sqlite() -> None:
    referencing = {_relative(path) for path, _, body in _pages() if any(word in body for word in SQLITE_WORDS)}
    assert referencing == SQLITE_DOCS
    assert all(any(word in body for word in SQLITE_WORDS) for _, _, body in _bodies())


# the snippets make their own databases in temporary directories, so none of them needs the working directory
@pytest.mark.parametrize(("path", "index", "body"), _bodies(), ids=lambda v: v.name if isinstance(v, Path) else None)
def test_snippet_runs(path: Path, index: int, body: str, tmp_path: Path) -> None:
    script = tmp_path / f"snippet_{path.stem}_{index}.py"
    script.write_text(body)
    run_path(str(script))
