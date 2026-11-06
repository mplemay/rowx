import ast
import asyncio
from collections import Counter, deque
from contextlib import AsyncExitStack, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from re import DOTALL, IGNORECASE, VERBOSE, Pattern, compile as re_compile, escape as re_escape, sub
from typing import TYPE_CHECKING, Final
from urllib.parse import quote, unquote

from rowx._backend import (
    CheckerBackend,
    ColumnInfo,
    Connection,
    Describe,
    UnsupportedSchemeError,
    backend_for,
)
from rowx.check.compare import ColumnDesc, DescribeDesc, Finding, compare, compare_param
from rowx.check.construction import template_reports
from rowx.check.discover import discover_source, too_deep
from rowx.check.model import Diagnostic, Rule, Span
from rowx.check.params import ParamInference
from rowx.check.render import Report, null_reason_note
from rowx.check.resolve import ModuleGraph, RowType, resolve_row_type, roots_for
from rowx.check.sourcemap import SourceMap, SourceMapper
from rowx.errors import ConnectionBusyError, DatabaseError, InterfaceError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Iterator
    from contextlib import AbstractAsyncContextManager
    from pathlib import Path

    from rowx.check.config import Config
    from rowx.check.model import FileResult, QueryCall

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
PASSWORD_PARAM: Final = re_compile(
    r"(?P<key>password\s*=\s*)(?P<value>'(?:[^'\\]|\\.)*'?|[^\s&]+)",
    flags=IGNORECASE | DOTALL,
)
# key=value form: a backslash escapes the next character and `&` is part of the value
PASSWORD_PARAM_KV: Final = re_compile(
    r"(?P<key>password\s*=\s*)(?P<value>'(?:[^'\\]|\\.)*'?|(?:\\.|[^\s\\])+)",
    flags=IGNORECASE | DOTALL,
)
URL_SCHEME: Final = re_compile(r"\s*postgres(?:ql)?://")
USERINFO: Final = re_compile(r"(?P<key>://[^/?#\s:@]*:)(?P<value>[^/?#\s]*)@")
_SELECTED: ContextVar[CheckerBackend | None] = ContextVar("rowx_checker_backend", default=None)


class CheckError(Exception):
    pass


@dataclass(slots=True, kw_only=True)
class Statistics:
    files: int = 0
    call_sites: int = 0
    checked: int = 0
    distinct_sql: int = 0
    unchecked: int = 0
    # None unless the drift gate ran
    migrations: int | None = None
    # None unless `--scratch` built a database
    scratch_seconds: float | None = None
    by_rule: Counter[Rule] = field(default_factory=Counter)


@dataclass(slots=True, kw_only=True, frozen=True)
class ScratchBuild:
    url: str
    seconds: float


# `gate` runs once, before anything is described, and only when there is something to describe
# `scratch` builds the database to describe against from the server URL, and drops it when the run ends
@dataclass(slots=True, kw_only=True, frozen=True)
class Hooks:
    gate: Callable[[str], Awaitable[int | None]] | None = None
    scratch: Callable[[str], AbstractAsyncContextManager[ScratchBuild]] | None = None


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


def _unescape(raw: str) -> str:
    # a backslash pair is its second character, and a bare quote is a delimiter
    return sub(r"\\(.)|'", r"\1", raw.removeprefix("'"), flags=DOTALL)


def _lower_percent(text: str) -> str:
    # only a hex letter right after a percent sign (or its first digit) changes
    return sub(r"(?<=%)[A-F]|(?<=%[0-9A-F])[A-F]", lambda match: match[0].lower(), text)


def _spellings(raw: str) -> set[str]:
    plain = _unescape(raw)
    escaped = sub(r"([\\'])", r"\\\1", plain)
    spaced = sub(r"(\s)", r"\\\1", escaped)
    encoded = quote(plain, safe="")
    return {raw, unquote(raw), plain, unquote(plain), encoded, _lower_percent(encoded), escaped, spaced}


# the decoded values the options parser finds, which covers a spelling the syntax scan above doesn't know
def _parsed_secrets(dsn: str) -> set[str]:
    if not dsn.strip():
        return set()
    try:
        options = backend_for(dsn).parse_dsn(dsn)
    except InterfaceError, UnsupportedSchemeError:
        return set()
    found: set[str] = {str(options.get("password") or "")}
    found.update(str(value) for key, value in options.get("extra", {}).items() if "password" in key)
    return found


def _password_param(text: str) -> Pattern[str]:
    return PASSWORD_PARAM if URL_SCHEME.match(text) else PASSWORD_PARAM_KV


def secrets_in(dsn: str) -> list[str]:
    raws = _parsed_secrets(dsn)
    for pattern in (_password_param(dsn), USERINFO):
        raws.update(match["value"] for match in pattern.finditer(dsn))
    found: set[str] = set()
    for raw in raws:
        found |= _spellings(raw)
    return sorted({secret for secret in found if secret}, key=len, reverse=True)


# URL userinfo and `password=` tokens are masked by syntax, so a DSN that can't be parsed is still covered
def _mask_syntax(text: str) -> str:
    text = USERINFO.sub(lambda match: f"{match['key']}***@", text)
    return _password_param(text).sub(lambda match: f"{match['key']}***", text)


def redact(text: str, dsn: str) -> str:
    # the whole URL goes first so a password is never left half-replaced
    text = text.replace(dsn, "<database url>") if dsn else text
    # a spelling longer than the text can't occur in it
    if not (secrets := [secret for secret in secrets_in(dsn) if len(secret) <= len(text)]):
        return _mask_syntax(text)
    # every occurrence of every spelling is found on the original text (a lookahead, so occurrences may overlap),
    # then overlapping and adjacent ones are merged and masked once
    pattern = re_compile("(?=(" + "|".join(re_escape(secret) for secret in secrets) + "))")
    out: list[str] = []
    cursor = 0
    span: list[int] | None = None
    for match in pattern.finditer(text):
        start, end = match.start(), match.end(1)
        if span is not None and start <= span[1]:
            span[1] = max(span[1], end)
            continue
        if span is not None:
            out.extend((text[cursor : span[0]], "***"))
            cursor = span[1]
        span = [start, end]
    if span is not None:
        out.extend((text[cursor : span[0]], "***"))
        cursor = span[1]
    out.append(text[cursor:])
    return _mask_syntax("".join(out))


def _selected_backend(dsn: str) -> CheckerBackend:
    if (backend := _SELECTED.get()) is not None:
        return backend
    return backend_for(dsn)


async def _connect(dsn: str, timeout_ms: int) -> Connection:
    try:
        return await _selected_backend(dsn).connect_checker(dsn, timeout_ms / 1000)
    except (InterfaceError, DatabaseError, OSError) as error:
        msg = f"could not connect to the database: {redact(str(error), dsn)}"
        raise CheckError(msg) from None


async def describe_impl(conn: Connection, sql: str) -> Describe:
    backend = _SELECTED.get()
    if backend is None:
        msg = "checker backend is not selected"
        raise CheckError(msg)
    return await backend.describe(conn, sql)


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


async def describe_all(
    dsn: str,
    statements: list[str],
    jobs: int,
    timeout_ms: int,
    backend: CheckerBackend | None = None,
) -> dict[str, Outcome]:
    selected = backend if backend is not None else backend_for(dsn)
    token = _SELECTED.set(selected)
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
    finally:
        _SELECTED.reset(token)
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
    return row_type.alias.notes()


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
    field: str | None = None,
    order: int = 0,
) -> Report:
    diagnostic = Diagnostic(rule=rule, message=message, path=site.call.path, span=span, notes=notes)
    return Report(diagnostic=diagnostic, help=help, label=label, column_index=column_index, field=field, order=order)


def _param_reports(site: _Site, described: Describe, backend: CheckerBackend) -> list[Report]:
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
    reports.extend(_param_type_reports(site, described, backend))
    return reports


def _param_type_reports(site: _Site, described: Describe, backend: CheckerBackend) -> list[Report]:
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
        if (
            finding := compare_param(index, param.type_name, param.type_key, found, param.element_key, backend)
        ) is not None:
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
        raw_name=column.raw_name,
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


def _row_reports(site: _Site, described: Describe, backend: CheckerBackend) -> list[Report]:
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
    return [
        _finding_report(site, finding, descs, order)
        for order, finding in enumerate(compare(DescribeDesc(columns=descs), row_type, backend))
    ]


def _finding_report(
    site: _Site,
    finding: Finding,
    columns: tuple[ColumnDesc, ...],
    order: int,
) -> Report:
    notes: list[str] = []
    label = None
    span = _row_span(site)
    elements = _element_spans(site)
    row_type = site.row_type
    named = row_type is not None and row_type.kind == "named"
    if finding.element is not None and elements is not None:
        span = elements[finding.element]
        label = f"element {finding.element + 1}"
    else:
        notes.extend(_alias_note(row_type))
    if named:
        label = "row type"
    if row_type is not None:
        for index in finding.fields:
            defined = row_type.fields[index]
            notes.append(f"field defined at {defined.path}:{defined.span.line}:{defined.span.col}")
            if defined.generic is not None:
                notes.append(defined.generic)
    name = columns[finding.column].name if finding.column is not None else None
    if reason := finding.reason:
        notes.append(null_reason_note(reason, finding.reason_detail, column=name))
    notes.extend(finding.notes)
    # `field` is set only for the rules that concern one field
    field_key = None
    if row_type is not None and len(finding.fields) == 1 and finding.rule != "ambiguous-column":
        field_key = row_type.fields[finding.fields[0]].key
    return _report(
        site,
        finding.rule,
        finding.message,
        span,
        notes=tuple(notes),
        help=finding.help,
        label=label,
        column_index=finding.column,
        field=field_key,
        order=order if named else 0,
    )


def _evaluate(site: _Site, outcome: Outcome, backend: CheckerBackend) -> list[Report]:
    if isinstance(outcome, DatabaseError):
        return [_sql_error(site, outcome)]
    return [
        *_param_reports(site, outcome, backend),
        *_row_reports(site, outcome, backend),
        *_override_reports(site, outcome),
    ]


def _file_sites(
    graph: ModuleGraph,
    roots: tuple[Path, ...],
    path: Path,
    result: FileResult,
    backend: CheckerBackend | None,
) -> tuple[list[Report], list[_Site]]:
    reports: list[Report] = []
    sites: list[_Site] = []
    inference = ParamInference(result.tree, backend) if result.tree is not None else None
    mapper = SourceMapper(result.source or "")
    reports.extend(template_reports(path, result.templates, mapper, inference))
    for call in result.calls:
        if call.template is None:
            continue
        resolution = resolve_row_type(graph, call, roots, backend)
        if resolution.diagnostic is not None:
            reports.append(Report(diagnostic=resolution.diagnostic, help=resolution.help))
        sites.append(
            _Site(
                call=call,
                mapper=mapper,
                sourcemap=mapper.build(call.template),
                row_type=resolution.row_type,
                inference=inference,
            ),
        )
    return reports, sites


def _collect(
    config: Config,
    files: Iterable[Path],
    stats: Statistics,
    backend: CheckerBackend | None,
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
        try:
            file_reports, file_sites = _file_sites(graph, roots, path, result, backend)
        except RecursionError:
            # fail closed for this file only: nothing else it produced is trusted
            reports.extend(Report(diagnostic=diagnostic) for diagnostic in too_deep(path).diagnostics)
            continue
        sources[path] = result.source
        stats.call_sites += len(result.calls)
        stats.checked += len(file_sites)
        reports.extend(file_reports)
        sites.extend(file_sites)
    return sites, reports, sources


async def run_check(  # ruff: ignore[too-many-arguments, too-many-positional-arguments]
    config: Config,
    files: Iterable[Path],
    dsn: str | None,
    jobs: int,
    hooks: Hooks | None = None,
    backend: CheckerBackend | None = None,
) -> RunResult:
    hooks = hooks or Hooks()
    stats = Statistics()
    selected = backend
    if selected is None and dsn is not None and dsn.strip():
        selected = backend_for(dsn)
    sites, reports, sources = _collect(config, files, stats, selected)
    statements = sorted({site.sourcemap.sql for site in sites})
    stats.distinct_sql = len(statements)
    if statements:
        if dsn is None or not dsn.strip():
            msg = f"no database URL; pass --database-url or set ${config.database_url_env}"
            raise CheckError(msg)
        if selected is None:
            selected = backend_for(dsn)
        target = dsn
        async with AsyncExitStack() as stack:
            if hooks.scratch is not None:
                built = await stack.enter_async_context(hooks.scratch(dsn))
                target = built.url
                stats.scratch_seconds = built.seconds
            if hooks.gate is not None:
                stats.migrations = await hooks.gate(target)
            outcomes = await describe_all(target, statements, jobs, config.statement_timeout_ms, selected)
        for site in sites:
            reports.extend(_evaluate(site, outcomes[site.sourcemap.sql], selected))
    stats.by_rule.update(report.diagnostic.rule for report in reports)
    return RunResult(reports=reports, sources=sources, statistics=stats)
