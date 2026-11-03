from typing import TYPE_CHECKING, cast

import pytest

from rowx.__tests__.conformance.row_types import PRELUDE, ROWS, UNKNOWN_NAMES, NamedPlan, Plan
from rowx.check.discover import discover_file
from rowx.check.resolve import ModuleGraph, resolve_row_type

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(("source", "expected", "verdict"), ROWS)
def test_conformance_row_checker_verdict(
    tmp_path: Path,
    source: str,
    expected: Plan | NamedPlan | str,
    verdict: str,
) -> None:
    path = tmp_path / "case.py"
    path.write_text(f'{PRELUDE}{source}\nfrom rowx import query\nquery[T](t"select 1")\n')
    [call] = discover_file(path).calls
    resolution = resolve_row_type(ModuleGraph(), call, (tmp_path,))
    if verdict in {"ok", UNKNOWN_NAMES}:
        assert resolution.diagnostic is None
        row = resolution.row_type
        assert row is not None
        assert not isinstance(expected, str)
        if expected[0] == "named":
            assert row.kind == "named"
            fields = tuple((f.key, f.names, f.elem.nullable, f.required) for f in row.fields)
            if verdict == UNKNOWN_NAMES:
                # the checker can't know any field's names, whatever the runtime resolves them to
                assert all(names is None for _, names, _, _ in fields)
                fields = tuple(
                    (key, expected_names, nullable, required)
                    for (key, _, nullable, required), (_, expected_names, _, _) in zip(
                        fields, cast("NamedPlan", expected)[3], strict=True
                    )
                )
            assert ("named", row.form, row.matching, fields) == expected
        else:
            assert (row.kind, len(row.elements), tuple(elem.nullable for elem in row.elements)) == (
                expected[0],
                expected[1],
                expected[2],
            )
    else:
        assert resolution.diagnostic is not None
        assert resolution.diagnostic.rule == verdict
