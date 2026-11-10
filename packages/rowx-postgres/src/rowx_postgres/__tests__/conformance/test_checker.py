from typing import TYPE_CHECKING, Final, cast

import pytest
from rowx_postgres.__tests__.conformance.row_types import (
    IMPORTED_TYPE_VARIABLE_CASES,
    PRELUDE,
    REJECTED,
    ROWS,
    TYPE_NAME_ROWS,
    UNKNOWN_NAMES,
    NamedPlan,
    Plan,
)
from rowx_postgres._backend import load

from rowx.check.discover import discover_file
from rowx.check.resolve import ModuleGraph, resolve_row_type

if TYPE_CHECKING:
    from pathlib import Path

    from rowx_core.backend import CheckerBackend

PG_RULES: Final[CheckerBackend] = load().checker()


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
    resolution = resolve_row_type(ModuleGraph(), call, (tmp_path,), backend=PG_RULES)
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


@pytest.mark.parametrize(("source", "type_name"), TYPE_NAME_ROWS)
def test_the_type_name_of_a_generic_row_is_the_canonical_text(tmp_path: Path, source: str, type_name: str) -> None:
    path = tmp_path / "case.py"
    path.write_text(f'{PRELUDE}{source}\nfrom rowx import query\nquery[T](t"select 1")\n')
    [call] = discover_file(path).calls
    resolution = resolve_row_type(ModuleGraph(), call, (tmp_path,), backend=PG_RULES)
    assert resolution.diagnostic is None
    assert resolution.row_type is not None
    assert resolution.row_type.type_name == type_name


@pytest.mark.parametrize(("name", "files", "expected"), IMPORTED_TYPE_VARIABLE_CASES)
def test_type_variables_imported_from_other_modules(
    tmp_path: Path, name: str, files: dict[str, str], expected: NamedPlan | str
) -> None:
    for filename, source in files.items():
        (tmp_path / filename).write_text(source)
    main = tmp_path / "main.py"
    main.write_text(f'{files["main.py"]}\nfrom rowx import query\nquery[T](t"select 1")\n')
    [call] = discover_file(main).calls
    resolution = resolve_row_type(ModuleGraph(), call, (tmp_path,), backend=PG_RULES)
    if isinstance(expected, str):
        assert resolution.diagnostic is not None, name
        assert resolution.diagnostic.rule == REJECTED
        return
    assert resolution.diagnostic is None, name
    assert resolution.row_type is not None
    assert tuple((f.key, f.names, f.elem.nullable, f.required) for f in resolution.row_type.fields) == expected[3]
