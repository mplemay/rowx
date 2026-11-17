from typing import TYPE_CHECKING

from rowx.check.config import Config
from rowx.check.resolve import roots_for

if TYPE_CHECKING:
    from pathlib import Path


def test_glob_include_uses_literal_prefix_for_roots(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    path = src / "queries.py"
    path.write_text("", encoding="utf-8")
    assert roots_for(Config(root=tmp_path, include=("src/**/*.py",)), path) == (src,)
    assert roots_for(Config(root=tmp_path, include=("**/*.py",)), path) == (tmp_path,)
    assert roots_for(Config(root=tmp_path, include=("src/queries.py",)), path) == (src,)
