import ast
import asyncio
from collections import Counter, deque
from contextlib import suppress
from dataclasses import dataclass, field
from re import DOTALL, IGNORECASE, VERBOSE, compile as re_compile
from typing import TYPE_CHECKING, Final
from urllib.parse import unquote, urlparse

from rowx._core import ColumnInfo, Connection, Describe, connect_checker, describe as describe_impl
from rowx.check.compare import ColumnDesc, DescribeDesc, Finding, compare, compare_param
from rowx.check.discover import discover_source
from rowx.check.model import Diagnostic, Rule, Span
from rowx.check.params import ParamInference
from rowx.check.render import Report, null_reason_note
from rowx.check.resolve import ModuleGraph, RowType, resolve_row_type, roots_for
from rowx.check.sourcemap import SourceMap, SourceMapper
from rowx.errors import ConnectionBusyError, DatabaseError, InterfaceError

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from pathlib import Path

    from rowx.check.config import Config
    from rowx.check.model import QueryCall

type Outcome = Describe | DatabaseError

IDENT_MAX_BYTES: Final[int] = 63
TIMEOUT_SQLSTATE: Final[str] = "57014"
SHUTDOWN_SQLSTATE: Final[str] = "57P01"
CONNECTION_CLASS: Final[str] = "08"
CAST_HELP: Final[dict[str, str]] = {
    "42P18": "cast the parameter: `{x}::int`",
    "42P08": "cast the parameter: `{x}::int`",
    "42501": "grant the checker's role the privileges this query needs, or run the check as a role that has them",
}
SQL_TOKEN: Final = re_compile(
    r"""
    (?P<space>\s+)
    |(?P<line_comment>--[^\n]*)
    |(?P<block_open>/\*)
    |(?P<string>[eE]?'(?:[^'\\]|\\.|'')*')
    |(?P<dollar>\$(?:[^\W\d]\w*)?\$)
    |(?P<quoted>"(?:[^"]|"")*")
    |(?P<word>[^\W\d]\w*)
    |(?P<other>.)
    """,
    flags=VERBOSE | DOTALL,
)
PASSWORD_PARAM: Final = re_compile(r"password\s*=\s*(?:'((?:[^'\\]|\\.)*)'|([^\s&]+))", flags=IGNORECASE)


class CheckError(Exception):
    pass


@dataclass(slots=True, kw_only=True)
class Statistics:
    files: int = 0
    call_sites: int = 0
    checked: int = 0
    distinct_sql: int = 0
    unchecked: int = 0
    by_rule: Counter[Rule] = field(default_factory=Counter)


@dataclass(slots=True, kw_only=True)
class RunResult:
    reports: list[Report]
    sources: dict[Path, str]
    statistics: Statistics


@dataclass(slots=True, kw_only=True)
class _Site:
    call: QueryCall
    mapper: SourceMapper
    sourcemap: SourceMap
    row_type: RowType | None
    inference: ParamInference | None


def secrets_in(dsn: str) -> list[str]:
    found: list[str] = []
    with suppress(ValueError):
        if (password := urlparse(dsn).password) is not None:
            found.extend([password, unquote(password)])
    found.extend(match[1] or match[2] for match in PASSWORD_PARAM.finditer(dsn))
    return sorted({secret for secret in found if secret}, key=len, reverse=True)


def redact(text: str, dsn: str) -> str:
    # the whole URL goes first so a password is never left half-replaced
    text = text.replace(dsn, "<database url>") if dsn else text
    for secret in secrets_in(dsn):
        text = text.replace(secret, "***")
    return text


async def _connect(dsn: str, timeout_ms: int) -> Connection:
    try:
        return await connect_checker(dsn, timeout_ms / 1000)
    except (InterfaceError, DatabaseError, OSError) as error:
        msg = f"could not connect to the database: {redact(str(error), dsn)}"
        raise CheckError(msg) from None


def _is_lost(error: DatabaseError) -> bool:
    return error.sqlstate == SHUTDOWN_SQLSTATE or error.sqlstate.startswith(CONNECTION_CLASS)


@dataclass(slots=True, frozen=True)
class _Lost:
    error: Exception


@dataclass(slots=True)
class _Worker:
    dsn: str
    timeout_ms: int
    conn: Connection | None = None

    async def _open(self) -> Connection:
        self.conn = await _connect(self.dsn, self.timeout_ms)
        return self.conn

    async def close(self) -> None:
        conn, self.conn = self.conn, None
        if conn is not None:
            with suppress(Exception):
                await conn.close()

    async def _try(self, sql: str) -> Outcome | _Lost:
        try:
            return await describe_impl(self.conn or await self._open(), sql)
        except ConnectionBusyError:
            raise
        except DatabaseError as error:
            if error.sqlstate == TIMEOUT_SQLSTATE:
                msg = f"describe timed out (statement_timeout): {error.message}"
                raise CheckError(msg) from None
            return error if not _is_lost(error) else _Lost(error)
        except InterfaceError as error:
            return _Lost(error)

    async def describe(self, sql: str) -> Outcome:
        # a lost connection is retried once on a fresh one
        outcome = await self._try(sql)
        if isinstance(outcome, _Lost):
            await self.close()
            outcome = await self._try(sql)
        if isinstance(outcome, _Lost):
            msg = f"lost the database connection: {redact(str(outcome.error), self.dsn)}"
            raise CheckError(msg)
        return outcome


async def describe_all(dsn: str, statements: list[str], jobs: int, timeout_ms: int) -> dict[str, Outcome]:
    pending = deque(statements)
    results: dict[str, Outcome] = {}

    async def run() -> None:
        worker = _Worker(dsn, timeout_ms)
        try:
            await worker._open()  # ruff: ignore[private-member-access]
            while pending:
                sql = pending.popleft()
                results[sql] = await worker.describe(sql)
        finally:
            await worker.close()

    try:
        async with asyncio.TaskGroup() as group:
            for _ in range(min(jobs, len(statements))):
                group.create_task(run())
    except* CheckError as errors:
        raise min(errors.exceptions, key=str) from None
    return results


def _element_spans(site: _Site) -> tuple[Span, ...] | None:
    node, row_type = site.call.row_type, site.row_type
    if row_type is None or row_type.kind != "tuple" or not isinstance(node, ast.Subscript):
        return None
    if not isinstance(node.value, ast.Name | ast.Attribute):
        return None
    if getattr(node.value, "id", getattr(node.value, "attr", "")) not in {"tuple", "Tuple"}:
        return None
    elements = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
    if len(elements) != len(row_type.elements):
        return None
    return tuple(site.mapper.span(element) for element in elements)


def _row_span(site: _Site) -> Span:
    return site.call.row_type_span or site.call.span


def _alias_note(row_type: RowType | None) -> tuple[str, ...]:
    if row_type is None or row_type.alias is None:
        return ()
    alias = row_type.alias
    return (f"alias defined at {alias.path}:{alias.span.line}:{alias.span.col}",)


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


def _sql_error(site: _Site, error: DatabaseError) -> Report:
    span = site.sourcemap.locate(error.position) if error.position is not None else site.mapper.span(_template(site))
    notes = tuple(
        note
        for note in (
            f"detail: {error.detail}" if error.detail is not None else None,
            f"hint: {error.hint}" if error.hint is not None else None,
        )
        if note is not None
    )
    return Report(
        diagnostic=Diagnostic(
            rule="sql-error",
            message=f"{error.message} (SQLSTATE {error.sqlstate})",
            path=site.call.path,
            span=span,
            notes=notes,
        ),
        help=CAST_HELP.get(error.sqlstate),
        sqlstate=error.sqlstate,
        sql=site.sourcemap.sql,
    )


def _template(site: _Site) -> ast.expr:
    template = site.call.template
    assert template is not None  # ruff: ignore[assert]
    return template


def _report(  # ruff: ignore[too-many-arguments]
    site: _Site,
    rule: Rule,
    message: str,
    span: Span,
    *,
    notes: tuple[str, ...] = (),
    help: str | None = None,  # ruff: ignore[builtin-argument-shadowing]
    label: str | None = None,
    column_index: int | None = None,
) -> Report:
    diagnostic = Diagnostic(rule=rule, message=message, path=site.call.path, span=span, notes=notes)
    return Report(diagnostic=diagnostic, help=help, label=label, column_index=column_index)


def _param_reports(site: _Site, described: Describe) -> list[Report]:
    holes = site.sourcemap.holes
    reports: list[Report] = []
    if (count := len(described.params)) != len(holes):
        # the server references $1..$count, so the first interpolation past that is the stray one
        span = holes[count] if count < len(holes) else site.mapper.span(_template(site))
        message = (
            f"the query has {_plural(len(holes), 'interpolation')} but the server expects {_plural(count, 'parameter')}"
        )
        help_ = "an interpolation inside a string literal or comment is not a parameter"
        reports.append(_report(site, "param-count-mismatch", message, span, help=help_))
    for index, param in enumerate(described.params):
        if param.type_key is None and index < len(holes):
            message = f'parameter ${index + 1} has unsupported type "{param.type_name}"'
            help_ = "cast the parameter to a supported type, such as `{x}::text`"
            reports.append(_report(site, "unsupported-type", message, holes[index], help=help_))
    reports.extend(_param_type_reports(site, described))
    return reports


def _param_type_reports(site: _Site, described: Describe) -> list[Report]:
    holes = site.sourcemap.holes
    # with a count mismatch the interpolations no longer line up with `$n`
    if site.inference is None or len(described.params) != len(holes):
        return []
    template = site.call.template
    if template is None:
        return []
    evident = site.inference.interpolations(template)
    if len(evident) != len(holes):
        return []
    reports: list[Report] = []
    for index, (param, found) in enumerate(zip(described.params, evident, strict=True)):
        if (finding := compare_param(index, param.type_name, param.type_key, found, param.element_key)) is not None:
            reports.append(_report(site, finding.rule, finding.message, holes[index]))
    return reports


def _column_desc(column: ColumnInfo) -> ColumnDesc:
    return ColumnDesc(
        name=column.name,
        type_name=column.type_name,
        type_key=column.type_key,
        nullable=column.nullable,
        reason=column.reason,
        reason_detail=column.reason_detail,
        element_key=column.element_key,
    )


def _skip_block_comment(sql: str, start: int) -> int:
    depth = 0
    index = start
    while index < len(sql):
        if sql.startswith("/*", index):
            depth += 1
            index += 2
        elif sql.startswith("*/", index):
            depth -= 1
            index += 2
            if depth == 0:
                return index
        else:
            index += 1
    return len(sql)


# quoted identifiers that follow `AS`, so text in comments, string literals and dollar quotes is never read as an alias
def quoted_aliases(sql: str) -> Iterator[tuple[int, str]]:
    index = 0
    previous = ""
    while (match := SQL_TOKEN.match(sql, index)) is not None:
        kind = match.lastgroup
        index = match.end()
        if kind == "block_open":
            index = _skip_block_comment(sql, match.start())
        elif kind == "dollar":
            index = len(sql) if (close := sql.find(match[0], index)) == -1 else close + len(match[0])
        elif kind == "quoted" and previous == "as":
            yield match.start(), match[0][1:-1].replace('""', '"')
        if kind not in {"space", "line_comment", "block_open"}:
            previous = match[0].lower() if kind == "word" else ""


def _truncated_alias(sql: str, column: ColumnInfo) -> int | None:
    # a described name that is a prefix of a longer `AS "…!"` alias lost its suffix to truncation
    # a cut at a character boundary can leave up to three bytes of room
    if not IDENT_MAX_BYTES - 4 < len(column.raw_name.encode()) <= IDENT_MAX_BYTES:
        return None
    for offset, ident in quoted_aliases(sql):
        if ident.endswith(("!", "?")) and len(ident.encode()) > IDENT_MAX_BYTES and ident.startswith(column.raw_name):
            return offset
    return None


def _override_reports(site: _Site, described: Describe) -> list[Report]:
    reports: list[Report] = []
    for index, column in enumerate(described.columns):
        if (offset := _truncated_alias(site.sourcemap.sql, column)) is None:
            continue
        message = (
            f'column {index + 1} "{column.raw_name}" was truncated at {IDENT_MAX_BYTES} bytes, '
            'so its "!" or "?" override is lost'
        )
        help_ = "shorten the alias so the name and its suffix fit in 63 bytes"
        reports.append(_report(site, "invalid-override", message, site.sourcemap.locate(offset + 1), help=help_))
    return reports


def _row_reports(site: _Site, described: Describe) -> list[Report]:
    row_type = site.row_type
    if row_type is None:
        return []
    columns = described.columns
    # a statement whose columns are all `void` returns nothing, as `SELECT pg_sleep(0)` does
    visible = [column for column in columns if column.type_key != "void"]
    span = _row_span(site)
    notes = _alias_note(row_type)
    if row_type.kind == "none":
        if not visible:
            return []
        message = f"the query returns {_plural(len(visible), 'column')} but the row type is `None`"
        return [_report(site, "missing-row-type", message, span, notes=notes, help="annotate the row type")]
    if not visible:
        message = "the query returns no columns but the row type is not `None`"
        return [_report(site, "unexpected-row-type", message, span, notes=notes, help="use .execute()")]
    descs = tuple(_column_desc(column) for column in columns)
    elements = _element_spans(site)
    return [
        _finding_report(site, finding, descs, elements, span)
        for finding in compare(DescribeDesc(columns=descs), row_type)
    ]


def _finding_report(
    site: _Site,
    finding: Finding,
    columns: tuple[ColumnDesc, ...],
    elements: tuple[Span, ...] | None,
    row_span: Span,
) -> Report:
    notes: list[str] = []
    label = None
    span = row_span
    if finding.element is not None and elements is not None:
        span = elements[finding.element]
        label = f"element {finding.element + 1}"
    else:
        notes.extend(_alias_note(site.row_type))
    name = columns[finding.column].name if finding.column is not None else None
    if reason := finding.reason:
        notes.append(null_reason_note(reason, finding.reason_detail, column=name))
    return _report(
        site,
        finding.rule,
        finding.message,
        span,
        notes=tuple(notes),
        help=finding.help,
        label=label,
        column_index=finding.column,
    )


def _evaluate(site: _Site, outcome: Outcome) -> list[Report]:
    if isinstance(outcome, DatabaseError):
        return [_sql_error(site, outcome)]
    return [*_param_reports(site, outcome), *_row_reports(site, outcome), *_override_reports(site, outcome)]


def _collect(
    config: Config,
    files: Iterable[Path],
    stats: Statistics,
) -> tuple[list[_Site], list[Report], dict[Path, str]]:
    graph = ModuleGraph()
    sites: list[_Site] = []
    reports: list[Report] = []
    sources: dict[Path, str] = {}
    for path in files:
        stats.files += 1
        result = discover_source(path, path.read_bytes())
        reports.extend(Report(diagnostic=diagnostic) for diagnostic in result.diagnostics)
        stats.unchecked += result.unchecked_count
        roots = roots_for(config, path)
        if result.source is None:
            continue
        inference = ParamInference(result.tree) if result.tree is not None else None
        sources[path] = result.source
        mapper = SourceMapper(result.source)
        for call in result.calls:
            stats.call_sites += 1
            if call.template is None:
                continue
            stats.checked += 1
            resolution = resolve_row_type(graph, call, roots)
            if resolution.diagnostic is not None:
                reports.append(Report(diagnostic=resolution.diagnostic))
            sites.append(
                _Site(
                    call=call,
                    mapper=mapper,
                    sourcemap=mapper.build(call.template),
                    row_type=resolution.row_type,
                    inference=inference,
                ),
            )
    return sites, reports, sources


async def run_check(config: Config, files: Iterable[Path], dsn: str | None, jobs: int) -> RunResult:
    stats = Statistics()
    sites, reports, sources = _collect(config, files, stats)
    statements = sorted({site.sourcemap.sql for site in sites})
    stats.distinct_sql = len(statements)
    if statements:
        if dsn is None or not dsn.strip():
            msg = f"no database URL; pass --database-url or set ${config.database_url_env}"
            raise CheckError(msg)
        outcomes = await describe_all(dsn, statements, jobs, config.statement_timeout_ms)
        for site in sites:
            reports.extend(_evaluate(site, outcomes[site.sourcemap.sql]))
    stats.by_rule.update(report.diagnostic.rule for report in reports)
    return RunResult(reports=reports, sources=sources, statistics=stats)
