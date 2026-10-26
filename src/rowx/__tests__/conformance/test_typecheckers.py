import json
import re
import subprocess  # ruff: ignore[suspicious-subprocess-import]
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import pytest

from rowx.__tests__.conformance.row_types import PRELUDE, ROWS
from rowx.check.discover import discover_file
from rowx.check.resolve import Elem, ModuleGraph, RowType, resolve_row_type

MAIN_NAME: Final[str] = "main.py"
REVEAL_MARK: Final[str] = "# reveal-here"
PYRIGHT_REVEAL: Final[re.Pattern[str]] = re.compile(r'^Type of ".*" is "(?P<type>.+)"$', re.DOTALL)
MYPY_LINE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+):\d+: (?P<severity>note|error): (?P<message>.*)$"
)
MYPY_REVEAL: Final[re.Pattern[str]] = re.compile(r'^Revealed type is "(?P<type>.+)"$')
# mypy qualifies classes with their module and pyright doesn't
MODULE_PREFIX: Final[re.Pattern[str]] = re.compile(r"\b(?:\w+\.)+(?=\w)")
FOOTER: Final[str] = f"""
from rowx import query

reveal_type(query[T](t"select 1"))  {REVEAL_MARK}
"""
# mypy rejects a variable assigned `None` as a type argument (`Variable "T" is not valid as a type`)
CHECKER_REJECTS: Final[frozenset[tuple[str, str]]] = frozenset({("T = None", "mypy")})
QUERY_TYPE: Final[re.Pattern[str]] = re.compile(r"^query\[(?P<row>.*)\]$", re.DOTALL)


@dataclass(slots=True, frozen=True, kw_only=True)
class PackageCase:
    name: str
    files: dict[str, str]
    main: str
    expected: str


# every case lives in its own package `c_<name>` so that module names never collide between cases
PACKAGE_CASES: Final[list[PackageCase]] = [
    PackageCase(
        name="absolute_alias",
        files={"defs.py": "type Pair = tuple[int, str | None]\n"},
        main="from c_absolute_alias.defs import Pair\n\nT = Pair\n",
        expected="tuple[int, str | None]",
    ),
    PackageCase(
        name="relative_alias",
        files={"defs.py": "type Pair = tuple[int, str | None]\n"},
        main="from .defs import Pair\n\nT = Pair\n",
        expected="tuple[int, str | None]",
    ),
    PackageCase(
        name="module_import",
        files={"defs.py": "type Pair = tuple[int, str | None]\n"},
        main="from c_module_import import defs as d\n\nT = d.Pair\n",
        expected="tuple[int, str | None]",
    ),
    PackageCase(
        name="reexport",
        files={"defs.py": "type Pair = tuple[int, str | None]\n", "__init__.py": "from .defs import Pair as Pair\n"},
        main="from c_reexport import Pair\n\nT = Pair\n",
        expected="tuple[int, str | None]",
    ),
    PackageCase(
        name="reexport_rename",
        files={
            "defs.py": "type Pair = tuple[int, str | None]\n",
            "api.py": "from .defs import Pair as Renamed\n",
        },
        main="from .api import Renamed as Row\n\nT = Row\n",
        expected="tuple[int, str | None]",
    ),
    PackageCase(
        name="type_checking",
        files={"defs.py": "type Pair = tuple[int, str | None]\n"},
        main="from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    from .defs import Pair\n\ntype T = Pair\n",
        expected="tuple[int, str | None]",
    ),
    PackageCase(
        name="scalar_chain",
        files={"defs.py": "type A = int | None\ntype B = A\n"},
        main="from .defs import B\n\nT = B\n",
        expected="int | None",
    ),
]


def _render_elem(elem: Elem) -> str:
    match elem.leaf:
        case "any":
            return "Any"
        case "none":
            return "None"
        case "list":
            if elem.item is None:
                return "list[Any]"
            base = f"list[{_render_elem(elem.item)}]"
        case leaf:
            # a network pair is one leaf written as its two classes, and a range as `Range[<subtype>]`
            base = MODULE_PREFIX.sub("", leaf)
    return f"{base} | None" if elem.nullable else base


def _render(row: RowType) -> str:
    match row.kind:
        case "none":
            return "None"
        case "scalar":
            return _render_elem(row.elements[0])
        case "tuple":
            return f"tuple[{', '.join(_render_elem(elem) for elem in row.elements)}]"


def _normalize(type_: str) -> str:
    stripped = MODULE_PREFIX.sub("", type_)
    # the revealed type is `query[<row type>]`; `object` and `Any` resolve to the same leaf
    row = match["row"] if (match := QUERY_TYPE.match(stripped)) else stripped
    return re.sub(r"\bobject\b", "Any", row)


type Key = tuple[str, int]


@dataclass(slots=True)
class Outcome:
    reveals: dict[Key, str] = field(default_factory=dict)
    errors: set[Key] = field(default_factory=set)


def _relative(root: Path, raw: str) -> str:
    path = Path(raw)
    return (path if path.is_absolute() else root / path).resolve().relative_to(root.resolve()).as_posix()


def _run_pyright(root: Path) -> Outcome:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pyright",
            "--outputjson",
            "--pythonversion",
            "3.14",
            "--pythonpath",
            sys.executable,
            ".",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=root,
    )
    outcome = Outcome()
    for diagnostic in json.loads(result.stdout)["generalDiagnostics"]:
        key = (_relative(root, diagnostic["file"]), diagnostic["range"]["start"]["line"] + 1)
        if diagnostic["severity"] == "error":
            outcome.errors.add(key)
        elif match := PYRIGHT_REVEAL.match(diagnostic["message"]):
            outcome.reveals[key] = _normalize(match["type"])
    return outcome


def _run_mypy(root: Path) -> Outcome:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--python-version",
            "3.14",
            "--python-executable",
            sys.executable,
            "--no-error-summary",
            "--show-column-numbers",
            "--no-incremental",
            "--explicit-package-bases",
            ".",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=root,
    )
    outcome = Outcome()
    for raw in result.stdout.splitlines():
        if not (match := MYPY_LINE.match(raw)):
            continue
        key = (_relative(root, match["file"]), int(match["line"]))
        if match["severity"] == "error":
            outcome.errors.add(key)
        elif reveal := MYPY_REVEAL.match(match["message"]):
            outcome.reveals[key] = _normalize(reveal["type"])
    return outcome


def _reveal_line(source: str) -> int:
    return next(number for number, line in enumerate(source.splitlines(), start=1) if REVEAL_MARK in line)


@dataclass(slots=True, frozen=True)
class Workspace:
    root: Path
    sources: dict[str, str]
    pyright: Outcome
    mypy: Outcome


@pytest.fixture(scope="module")
def workspace(tmp_path_factory: pytest.TempPathFactory) -> Workspace:
    root = tmp_path_factory.mktemp("typecheckers")
    sources: dict[str, str] = {}
    for index, (source, _, _) in enumerate(ROWS):
        sources[f"row_{index}.py"] = f"{PRELUDE}{source}\n{FOOTER}"
    for case in PACKAGE_CASES:
        package = f"c_{case.name}"
        files = {"__init__.py": "", **case.files}
        for name, text in files.items():
            sources[f"{package}/{name}"] = text
        sources[f"{package}/{MAIN_NAME}"] = f"{PRELUDE}{case.main}\n{FOOTER}"
    for name, text in sources.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return Workspace(root, sources, _run_pyright(root), _run_mypy(root))


def _resolve(root: Path, name: str) -> RowType | str:
    [call] = discover_file(root / name).calls
    resolution = resolve_row_type(ModuleGraph(), call, (root,))
    if resolution.row_type is not None:
        return resolution.row_type
    assert resolution.diagnostic is not None
    return resolution.diagnostic.rule


@pytest.mark.parametrize("checker", ["pyright", "mypy"])
@pytest.mark.parametrize(("index", "row"), list(enumerate(ROWS)), ids=[f"row_{i}" for i in range(len(ROWS))])
def test_conformance_row_matches_type_checker(
    workspace: Workspace,
    checker: str,
    index: int,
    row: tuple[str, object, str],
) -> None:
    name = f"row_{index}.py"
    key = (name, _reveal_line(workspace.sources[name]))
    outcome = getattr(workspace, checker)
    resolved = _resolve(workspace.root, name)
    if (row[0], checker) in CHECKER_REJECTS:
        # a divergence in the type checker, not in rowx: pinned so that a change on either side shows up
        assert isinstance(resolved, RowType)
        assert key in outcome.errors
    elif row[2] == "ok":
        assert isinstance(resolved, RowType)
        assert key not in outcome.errors
        assert outcome.reveals[key] == _render(resolved)
    else:
        assert resolved == row[2]
        # the checker must have seen the row, either as a type or as an error
        assert key in outcome.reveals or key in outcome.errors or any(k[0] == name for k in outcome.errors)


@pytest.mark.parametrize("checker", ["pyright", "mypy"])
@pytest.mark.parametrize("case", PACKAGE_CASES, ids=[case.name for case in PACKAGE_CASES])
def test_conformance_package_case_matches_type_checker(workspace: Workspace, checker: str, case: PackageCase) -> None:
    name = f"c_{case.name}/{MAIN_NAME}"
    key = (name, _reveal_line(workspace.sources[name]))
    outcome = getattr(workspace, checker)
    resolved = _resolve(workspace.root, name)
    assert isinstance(resolved, RowType)
    assert _render(resolved) == case.expected
    assert outcome.reveals[key] == case.expected
