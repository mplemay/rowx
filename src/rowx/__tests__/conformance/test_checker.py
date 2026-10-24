from typing import TYPE_CHECKING

import pytest

from rowx.__tests__.conformance.row_types import PRELUDE, ROWS
from rowx.check.discover import discover_file
from rowx.check.resolve import ModuleGraph, resolve_row_type

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(("source", "expected", "verdict"), ROWS)
def test_conformance_row_checker_verdict(
    tmp_path: Path,
    source: str,
    expected: tuple[str, int, tuple[bool, ...]] | str,
    verdict: str,
) -> None:
    path = tmp_path / "case.py"
    path.write_text(f'{PRELUDE}{source}\nfrom rowx import query\nquery[T](t"select 1")\n')
    [call] = discover_file(path).calls
    resolution = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    if verdict == "ok":
        assert resolution.diagnostic is None
        assert (row := resolution.row_type) is not None
        assert not isinstance(expected, str)
        assert (row.kind, len(row.elements), tuple(elem.nullable for elem in row.elements)) == (
            expected[0],
            expected[1],
            expected[2],
        )
    else:
        assert resolution.diagnostic is not None
        assert resolution.diagnostic.rule == verdict
