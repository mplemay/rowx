import io
import re
import tokenize
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from rowx.check.model import Diagnostic, Rule, Span
from rowx.check.render import Report, sort_reports
from rowx.check.rules import RULES

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(slots=True, kw_only=True)
class Directive:
    path: Path
    line: int
    col: int
    header: bool
    standalone: bool
    codes: list[str] | None
    used: set[int] = field(default_factory=set)


def _diagnostic(path: Path, line: int, col: int, rule: Rule, message: str) -> Report:
    return Report(
        diagnostic=Diagnostic(
            rule=rule, message=message, path=path, span=Span(line=line, col=col, end_line=line, end_col=col + 1)
        )
    )


def _comment(
    path: Path, token: tokenize.TokenInfo, *, standalone: bool, begun: bool
) -> tuple[list[Directive], list[Report]]:
    directives: list[Directive] = []
    errors: list[Report] = []
    line, offset = token.start
    for match in re.finditer(r"#\s*rowx\s*:\s*ignore([^#]*)(?=#|$)", token.string):
        raw_tail = match[1]
        col = offset + match.start() + 1
        if match.end() < len(token.string) and (not raw_tail or not raw_tail[-1].isspace()):
            errors.append(_diagnostic(path, line, col, "invalid-ignore-comment", "reason must follow whitespace"))
            continue
        tail = raw_tail.strip()
        codes: list[str] | None = None
        if tail:
            bracket = re.fullmatch(r"\[([^\]]*)\]\s*", tail)
            if bracket is None or not bracket[1].strip():
                errors.append(_diagnostic(path, line, col, "invalid-ignore-comment", "malformed ignore comment"))
                continue
            codes = [code.strip() for code in bracket[1].split(",")]
            if any(not code for code in codes):
                errors.append(_diagnostic(path, line, col, "invalid-ignore-comment", "malformed ignore list"))
                continue
            errors.extend(
                _diagnostic(path, line, col, "invalid-ignore-comment", f"unknown rule {code!r}")
                for code in codes
                if code not in RULES
            )
        directives.append(
            Directive(
                path=path, line=line, col=col, header=standalone and not begun, standalone=standalone, codes=codes
            )
        )
    return directives, errors


def _parse(path: Path, source: str) -> tuple[list[Directive], list[Report]]:
    directives: list[Directive] = []
    errors: list[Report] = []
    source = source.replace("\r\n", "\n").replace("\r", "\n")
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except tokenize.TokenError, IndentationError, SyntaxError:
        return [], []
    begun = False
    lines = source.split("\n")
    for token in tokens:
        if token.type == tokenize.COMMENT:
            line, offset = token.start
            found, invalid = _comment(path, token, standalone=not lines[line - 1][:offset].strip(), begun=begun)
            directives.extend(found)
            errors.extend(invalid)
        elif token.type not in {
            tokenize.NL,
            tokenize.NEWLINE,
            tokenize.INDENT,
            tokenize.DEDENT,
            tokenize.ENDMARKER,
            tokenize.ENCODING,
        }:
            begun = True
    return directives, errors


def _matches(directive: Directive, report: Report) -> int | None:
    diagnostic = report.diagnostic
    span = diagnostic.span
    end = span.end_line - 1 if span.end_line > span.line and span.end_col == 1 else span.end_line
    if directive.path != diagnostic.path or not (
        directive.header or (not directive.standalone and directive.line in {span.line, end})
    ):
        return None
    if directive.codes is None:
        return None if diagnostic.rule == "unused-ignore-comment" else 0
    return next((i for i, code in enumerate(directive.codes) if code == diagnostic.rule), None)


def apply_suppressions(reports: list[Report], sources: dict[Path, str], levels: dict[str, str]) -> list[Report]:
    directives: list[Directive] = []
    findings: list[Report] = []
    for path, source in sources.items():
        parsed, errors = _parse(path, source)
        directives.extend(parsed)
        if levels["invalid-ignore-comment"] != "ignore":
            findings.extend(errors)
    directives.sort(key=lambda item: (item.header, item.line, item.col))
    visible: list[Report] = []
    for report in sort_reports([*reports, *findings]):
        if report.operational:
            visible.append(report)
            continue
        for directive in directives:
            index = _matches(directive, report)
            if index is not None:
                directive.used.add(index)
                break
        else:
            visible.append(report)
    if levels["unused-ignore-comment"] != "ignore":
        visible.extend(
            report
            for report in _unused(directives)
            if not any(_matches(directive, report) is not None for directive in directives)
        )
    return visible


def _unused(directives: list[Directive]) -> list[Report]:
    unused: list[Report] = []
    for directive in directives:
        entries = [None] if directive.codes is None else directive.codes
        for index, code in enumerate(entries):
            if index not in directive.used and (code is None or code in RULES) and code != "unused-ignore-comment":
                unused.append(
                    _diagnostic(
                        directive.path,
                        directive.line,
                        directive.col,
                        "unused-ignore-comment",
                        f"unused ignore{f' for {code}' if code is not None else ''}",
                    )
                )
    return unused
