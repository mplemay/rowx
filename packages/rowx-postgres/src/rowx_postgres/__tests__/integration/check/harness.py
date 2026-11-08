import asyncio
from dataclasses import dataclass
from pathlib import Path
from re import compile as re_compile
from typing import TYPE_CHECKING, Final

from rowx.check.config import load_config
from rowx.check.runner import run_check
from rowx.check.walk import discover_files

if TYPE_CHECKING:
    from collections.abc import Iterable

    from rowx.check.render import Report

CASES_DIR: Final[Path] = Path(__file__).resolve().parent / "cases"
MARKER: Final = re_compile(r"#\s*error:\s*\[([^\]]*)\]")


@dataclass(slots=True, frozen=True, order=True)
class Expectation:
    line: int
    rule: str


def parse_markers(source: str) -> list[Expectation]:
    found: list[Expectation] = []
    for number, line in enumerate(source.splitlines(), start=1):
        if (match := MARKER.search(line)) is not None:
            found.extend(Expectation(number, rule.strip()) for rule in match[1].split(",") if rule.strip())
    return sorted(found)


def actual_diagnostics(reports: Iterable[Report]) -> list[Expectation]:
    return sorted(Expectation(report.diagnostic.span.line, report.diagnostic.rule) for report in reports)


def diff(expected: list[Expectation], actual: list[Expectation]) -> list[str]:
    remaining = list(actual)
    problems: list[str] = []
    for item in expected:
        if item in remaining:
            remaining.remove(item)
        else:
            problems.append(f"line {item.line}: expected [{item.rule}] but it was not reported")
    problems.extend(f"line {item.line}: unexpected [{item.rule}]" for item in remaining)
    return problems


def check_case(path: Path, database_url: str) -> list[str]:
    config = load_config(path.parent)
    files = discover_files(config, path.parent, [path])
    result = asyncio.run(run_check(config, files, database_url, 1))
    return diff(parse_markers(path.read_text(encoding="utf-8")), actual_diagnostics(result.reports))
