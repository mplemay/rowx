from pathlib import Path
from subprocess import run  # ruff: ignore[suspicious-subprocess-import]
from sys import executable
from typing import Final

ROOT: Final[Path] = next(parent for parent in Path(__file__).resolve().parents if (parent / "Cargo.lock").is_file())


def test_types_doc_is_current() -> None:
    result = run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [executable, str(ROOT / "scripts" / "gen-types-doc.py"), "--check"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
