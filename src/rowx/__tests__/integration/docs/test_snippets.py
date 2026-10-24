from pathlib import Path
from re import DOTALL, MULTILINE, compile as re_compile
from runpy import run_path
from subprocess import run  # ruff: ignore[suspicious-subprocess-import]
from sys import executable
from typing import Final

import pytest

ROOT: Final[Path] = Path(__file__).resolve().parents[5]
DOC_FILES: Final[tuple[Path, ...]] = (ROOT / "README.md", ROOT / "docs" / "guide.md")
SNIPPET: Final = re_compile(r"^```python test\n(?P<body>.*?)^```$", MULTILINE | DOTALL)


def _snippets() -> list[tuple[Path, int, str]]:
    return [(path, i, m["body"]) for path in DOC_FILES for i, m in enumerate(SNIPPET.finditer(path.read_text()))]


def test_every_doc_has_snippets() -> None:
    assert {path for path, _, _ in _snippets()} == set(DOC_FILES)


@pytest.mark.integration
@pytest.mark.db
@pytest.mark.parametrize(("path", "index", "body"), _snippets(), ids=lambda v: v.name if isinstance(v, Path) else None)
def test_snippet_runs(path: Path, index: int, body: str, schema: str, tmp_path: Path) -> None:
    script = tmp_path / f"snippet_{path.stem}_{index}.py"
    script.write_text(body)
    run_path(str(script), init_globals={"DSN": schema})


def test_types_doc_is_current() -> None:
    result = run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [executable, str(ROOT / "scripts" / "gen-types-doc.py"), "--check"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
