import ast
import asyncio
from collections import Counter, deque
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass, field, replace
from re import DOTALL, IGNORECASE, Pattern, compile as re_compile, escape as re_escape, sub
from typing import TYPE_CHECKING, Final, Literal
from urllib.parse import parse_qsl, quote, unquote, urlsplit

from rowx_core.backend import (
    CheckerBackend,
    CheckerSession,
    ColumnInfo,
    Describe,
    UnsupportedCapabilityError,
)
from rowx_core.errors import ConnectionBusyError, DatabaseError, InterfaceError

from rowx._backend import BackendMissing, UnsupportedSchemeError, backend_for
from rowx.check.compare import (
    ColumnDesc,
    DescribeDesc,
    Finding,
    compare,
    compare_param_result,
    expected_type,
    shown_param,
)
from rowx.check.construction import template_reports
from rowx.check.discover import discover_source, too_deep
from rowx.check.model import Diagnostic, Rule, Span
from rowx.check.params import ParamInference
from rowx.check.render import Report, null_reason_note, strip_controls
from rowx.check.resolve import ModuleGraph, RowType, resolve_row_type, roots_for
from rowx.check.sourcemap import SourceMap, SourceMapper

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Sequence
    from contextlib import AbstractAsyncContextManager
    from pathlib import Path

    from rowx_core.backend import ScratchBuild

    from rowx.check.config import Config
    from rowx.check.model import FileResult, QueryCall, QueryKind
    from rowx.check.params import ParamType

type Outcome = Describe | DatabaseError | _Operational
type RowComparison = Literal["compared", "partially compared", "not compared"]

PASSWORD_PARAM: Final[Pattern[str]] = re_compile(
    r"(?P<key>password\s*=\s*)(?P<value>'(?:[^'\\]|\\.)*'?|[^\s&]+)",
    flags=IGNORECASE | DOTALL,
)
# key=value form: a backslash escapes the next character and `&` is part of the value
PASSWORD_PARAM_KV: Final[Pattern[str]] = re_compile(
    r"(?P<key>password\s*=\s*)(?P<value>'(?:[^'\\]|\\.)*'?|(?:\\.|[^\s\\])+)",
    flags=IGNORECASE | DOTALL,
)
URL_SCHEME: Final[Pattern[str]] = re_compile(r"\s*[A-Za-z][A-Za-z0-9+.-]*://")
USERINFO: Final[Pattern[str]] = re_compile(r"(?P<key>://[^/?#\s:@]*:)(?P<value>[^/?#\s]*)@")
ROW_INCOMPLETE_RULES: Final[frozenset[Rule]] = frozenset({
    "type-mismatch",
    "unsupported-type",
    "arity-mismatch",
    "unexpected-column",
    "missing-column",
    "duplicate-column",
    "ambiguous-column",
    "column-name-mismatch",
})


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


# `gate` runs once, before anything is described, and only when there is something to describe; it gets the first URL
# `scratch` builds the database to describe against from the target URL, for that many workers, and tears it down when
# the run ends
# `own` starts a database that the run owns and yields its URL; the run then describes with exactly one worker
@dataclass(slots=True, kw_only=True, frozen=True)
class Hooks:
    gate: Callable[[str], Awaitable[int | None]] | None = None
    scratch: Callable[[str, int], AbstractAsyncContextManager[ScratchBuild]] | None = None
    own: Callable[[str], AbstractAsyncContextManager[str]] | None = None


@dataclass(slots=True, kw_only=True, frozen=True)
class ParameterCoverage:
    span: Span | None
    sql_type: str
    sql_type_key: str | None
    sql_element_key: str | None
    python_type: ParamType | None
    # None means the available evidence cannot establish a complete comparison.
    mismatch: bool | None


@dataclass(slots=True, kw_only=True, frozen=True)
class InspectedColumn:
    name: str
    sql_type: str
    python_type: str | None
    nullable: bool
    reason: str


@dataclass(slots=True, kw_only=True)
class SiteCoverage:
    path: Path
    span: Span
    kind: QueryKind
    described: bool = False
    row_comparison: RowComparison = "not compared"
    parameters_aligned: bool = False
    parameters: tuple[ParameterCoverage, ...] = ()
    sql_failed: bool = False
    operational_failure: bool = False
    row_failed: bool = False
    row_unresolved: bool = False
    parameters_failed: bool = False


@dataclass(slots=True, kw_only=True, frozen=True)
class InspectedParameter:
    sql_type: str
    python_type: str | None
    mismatch: bool | None


@dataclass(slots=True, kw_only=True, frozen=True)
class InspectedSite:
    path: str
    span: Span
    columns: tuple[InspectedColumn, ...]
    parameters: tuple[InspectedParameter, ...]


@dataclass(slots=True, kw_only=True)
class RunResult:
    reports: list[Report]
    sources: dict[Path, str]
    statistics: Statistics
    coverage: list[SiteCoverage] = field(default_factory=list)
    inspection: list[InspectedSite] = field(default_factory=list)


@dataclass(slots=True, kw_only=True)
class _Site:
    call: QueryCall
    mapper: SourceMapper
    sourcemap: SourceMap
    row_type: RowType | None
    inference: ParamInference | None
    coverage: SiteCoverage


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
    try:
        parts = urlsplit(dsn)
        found = {parts.password or ""}
        found.update(value for key, value in parse_qsl(parts.query) if "password" in key.lower())
    except ValueError:
        return set()
    else:
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


async def _connect(dsn: str, timeout_ms: int, backend: CheckerBackend) -> CheckerSession:
    try:
        return await backend.connect_checker(dsn, timeout_ms / 1000)
    except (InterfaceError, DatabaseError, OSError) as error:
        msg = f"could not connect to the database: {redact(str(error), dsn)}"
        raise CheckError(msg) from None


@dataclass(slots=True, frozen=True)
class _Operational:
    error: DatabaseError


@dataclass(slots=True, frozen=True)
class _Lost:
    error: Exception


@dataclass(slots=True)
class _Worker:
    dsn: str
    timeout_ms: int
    backend: CheckerBackend
    conn: CheckerSession | None = None

    async def _open(self) -> CheckerSession:
        self.conn = await _connect(self.dsn, self.timeout_ms, self.backend)
        return self.conn

    async def close(self) -> None:
        conn, self.conn = self.conn, None
        if conn is not None:
            with suppress(Exception):
                await conn.close()

    async def _try(self, sql: str) -> Outcome | _Lost:
        session = self.conn or await self._open()
        try:
            return await session.describe(sql)
        except ConnectionBusyError:
            raise
        except (DatabaseError, InterfaceError) as error:
            match session.classify(error):
                case "timeout":
                    msg = f"describe timed out: {error}"
                    raise CheckError(msg) from None
                case "lost":
                    return _Lost(error)
                case "operational" if isinstance(error, DatabaseError):
                    return _Operational(error)
                case _:
                    if isinstance(error, DatabaseError):
                        return error
                    raise

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


# one worker per URL (at most one per statement), so a URL that appears twice is shared by two workers
async def describe_all(
    urls: Sequence[str],
    statements: list[str],
    timeout_ms: int,
    backend: CheckerBackend | None = None,
) -> dict[str, Outcome]:
    selected = backend if backend is not None else backend_for(urls[0]).checker()
    pending = deque(statements)
    results: dict[str, Outcome] = {}

    async def run(url: str) -> None:
        worker = _Worker(url, timeout_ms, selected)
        try:
            await worker._open()  # ruff: ignore[private-member-access]
            while pending:
                sql = pending.popleft()
                results[sql] = await worker.describe(sql)
        finally:
            await worker.close()

    try:
        async with asyncio.TaskGroup() as group:
            for url in urls[: len(statements)]:
                group.create_task(run(url))
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
    return row_type.alias.notes()


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


def _sql_error(site: _Site, error: DatabaseError, backend: CheckerBackend) -> Report:
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
        help=backend.sql_error_help.get(error.sqlstate),
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
    wider_nullability: bool = False,
) -> Report:
    diagnostic = Diagnostic(rule=rule, message=message, path=site.call.path, span=span, notes=notes)
    return Report(
        diagnostic=diagnostic,
        help=help,
        label=label,
        column_index=column_index,
        field=field,
        order=order,
        wider_nullability=wider_nullability,
    )


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
    # Count mismatches make positional pairing unreliable.
    template = site.call.template
    evident = (
        site.inference.interpolations(template)
        if site.inference is not None and template is not None and len(described.params) == len(holes)
        else None
    )
    aligned = evident is not None and len(described.params) == len(holes) == len(evident)
    site.coverage.parameters_aligned = aligned
    reports: list[Report] = []
    facts: list[ParameterCoverage] = []
    for index, param in enumerate(described.params):
        found = evident[index] if aligned else None
        finding, complete = compare_param_result(
            index, param.type_name, param.type_key, found, param.element_key, backend
        )
        span = holes[index] if aligned else None
        facts.append(
            ParameterCoverage(
                span=span,
                sql_type=param.type_name,
                sql_type_key=param.type_key,
                sql_element_key=param.element_key,
                python_type=found,
                mismatch=(finding is not None) if complete else None,
            )
        )
        if finding is not None and span is not None:
            reports.append(_report(site, finding.rule, finding.message, span))
    site.coverage.parameters = tuple(facts)
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


def _override_reports(site: _Site, described: Describe, backend: CheckerBackend) -> list[Report]:
    if backend.override_issues is None:
        return []
    return [
        _report(site, "invalid-override", issue.message, site.sourcemap.locate(issue.offset + 1), help=issue.help)
        for issue in backend.override_issues(site.sourcemap.sql, described.columns)
    ]


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
        site.coverage.row_comparison = "partially compared" if visible else "compared"
        if not visible:
            return []
        message = f"the query returns {_plural(len(visible), 'column')} but the row type is `None`"
        return [_report(site, "missing-row-type", message, span, notes=notes, help="annotate the row type")]
    if not visible:
        site.coverage.row_comparison = "partially compared"
        message = "the query returns no columns but the row type is not `None`"
        return [_report(site, "unexpected-row-type", message, span, notes=notes, help="use .execute()")]
    descs = tuple(_column_desc(column) for column in columns)
    findings = compare(DescribeDesc(columns=descs), row_type, backend)
    incomplete = any(finding.rule in ROW_INCOMPLETE_RULES for finding in findings) or (
        row_type.kind == "named" and any(field.names is None for field in row_type.fields)
    )
    site.coverage.row_unresolved = row_type.kind == "named" and any(field.names is None for field in row_type.fields)
    site.coverage.row_comparison = "partially compared" if incomplete else "compared"
    return [_finding_report(site, finding, descs, order) for order, finding in enumerate(findings)]


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
        wider_nullability=finding.wider_nullability,
    )


def _evaluate(site: _Site, outcome: Outcome, backend: CheckerBackend) -> list[Report]:
    if isinstance(outcome, (DatabaseError, _Operational)):
        site.coverage.sql_failed = True
        site.coverage.operational_failure = isinstance(outcome, _Operational)
        error = outcome.error if isinstance(outcome, _Operational) else outcome
        return [replace(_sql_error(site, error, backend), operational=isinstance(outcome, _Operational))]
    site.coverage.described = True
    params = _param_reports(site, outcome, backend)
    rows = _row_reports(site, outcome, backend)
    site.coverage.parameters_failed = any(report.diagnostic.rule == "param-count-mismatch" for report in params)
    site.coverage.row_failed = any(not report.wider_nullability for report in rows)
    return [
        *params,
        *rows,
        *_override_reports(site, outcome, backend),
    ]


def _inspection(
    site: _Site, outcome: Describe, backend: CheckerBackend, dsns: tuple[str, ...], root: Path
) -> InspectedSite:
    def clean(value: str) -> str:
        value = strip_controls(value)
        for dsn in dsns:
            value = redact(value, dsn)
        return value

    columns: list[InspectedColumn] = []
    for column in outcome.columns:
        key = column.type_key
        supported = key in backend.types and (key != "array" or column.element_key in backend.types)
        mapping = expected_type(_column_desc(column), backend) if supported else None
        reason = (
            "nullability unknown, assumed nullable"
            if column.reason == "Computed"
            else null_reason_note(column.reason, column.reason_detail, column=column.name)
        )
        columns.append(
            InspectedColumn(
                name=clean(column.name),
                sql_type=clean(column.type_name),
                python_type=clean(mapping) if bool(mapping) else None,
                nullable=column.nullable,
                reason=clean(reason),
            )
        )
    parameters = tuple(
        InspectedParameter(
            sql_type=clean(param.sql_type),
            python_type=clean(shown_param(param.python_type)) if param.python_type is not None else None,
            mismatch=param.mismatch,
        )
        for param in site.coverage.parameters
    )
    try:
        path = site.call.path.relative_to(root)
    except ValueError:
        path = site.call.path
    return InspectedSite(path=clean(str(path)), span=site.call.span, columns=tuple(columns), parameters=parameters)


# an unchecked file is not described; the type table is vocabulary and opening it does not connect
def _vocabulary_backend(dsn: str | None) -> CheckerBackend | None:
    supplied = (dsn,) if dsn is not None and dsn.strip() else ()
    for url in (*supplied, "postgres://"):
        with suppress(BackendMissing, UnsupportedSchemeError, UnsupportedCapabilityError):
            return backend_for(url).checker()
    return None


def _file_sites(  # ruff: ignore[too-many-arguments, too-many-positional-arguments]
    graph: ModuleGraph,
    roots: tuple[Path, ...],
    path: Path,
    result: FileResult,
    backend: CheckerBackend | None,
    dsn: str | None,
    coverage: dict[Span, SiteCoverage],
) -> tuple[list[Report], list[_Site]]:
    reports: list[Report] = []
    sites: list[_Site] = []
    inference = ParamInference(result.tree, backend, dsn) if result.tree is not None else None
    mapper = SourceMapper(result.source or "", None if backend is None else backend.compiler)
    reports.extend(template_reports(path, result.templates, mapper, inference))
    # one vocabulary load for the file; a URL that cannot be loaded must not reach resolve_row_type
    row_backend = backend if backend is not None else _vocabulary_backend(dsn)
    row_dsn = dsn if backend is not None else None
    for call in result.calls:
        resolution = resolve_row_type(graph, call, roots, row_backend, row_dsn)
        if resolution.diagnostic is not None:
            reports.append(Report(diagnostic=resolution.diagnostic, help=resolution.help))
        if call.template is None:
            continue
        sites.append(
            _Site(
                call=call,
                mapper=mapper,
                sourcemap=mapper.build(call.template),
                row_type=resolution.row_type,
                inference=inference,
                coverage=coverage[call.span],
            ),
        )
    return reports, sites


def _collect(  # ruff: ignore[too-many-arguments, too-many-positional-arguments]
    config: Config,
    files: Iterable[Path],
    stats: Statistics,
    backend: CheckerBackend | None,
    dsn: str | None = None,
    coverage: list[SiteCoverage] | None = None,
    boundary: Path | None = None,
) -> tuple[list[_Site], list[Report], dict[Path, str], CheckerBackend | None]:
    graph = ModuleGraph(boundary)
    sites: list[_Site] = []
    reports: list[Report] = []
    sources: dict[Path, str] = {}
    for path in files:
        stats.files += 1
        result = discover_source(path, path.read_bytes())
        reports.extend(Report(diagnostic=diagnostic) for diagnostic in result.diagnostics)
        stats.unchecked += result.unchecked_count
        file_coverage = {span: SiteCoverage(path=path, span=span, kind=kind) for kind, span in result.recognized}
        if coverage is not None:
            coverage.extend(file_coverage.values())
        roots = roots_for(config, path)
        if result.source is None:
            continue
        sources[path] = result.source
        selected = backend
        if selected is None and any(call.template is not None for call in result.calls):
            if dsn is None or not dsn.strip():
                msg = f"no database URL; pass --database-url or set ${config.database_url_env}"
                raise CheckError(msg)
            selected = backend_for(dsn).checker()
            backend = selected
        try:
            file_reports, file_sites = _file_sites(graph, roots, path, result, selected, dsn, file_coverage)
        except RecursionError:
            # fail closed for this file only: nothing else it produced is trusted
            reports.extend(Report(diagnostic=diagnostic) for diagnostic in too_deep(path).diagnostics)
            continue
        sources[path] = result.source
        stats.call_sites += len(result.calls)
        stats.checked += len(file_sites)
        reports.extend(file_reports)
        sites.extend(file_sites)
    return sites, reports, sources, backend


async def run_check(  # ruff: ignore[too-many-arguments, too-many-positional-arguments]
    config: Config,
    files: Iterable[Path],
    dsn: str | None,
    jobs: int,
    hooks: Hooks | None = None,
    backend: CheckerBackend | None = None,
    *,
    inspect: bool = False,
    boundary: Path | None = None,
) -> RunResult:
    hooks = hooks or Hooks()
    stats = Statistics()
    selected = backend
    coverage: list[SiteCoverage] = []
    inspection: list[InspectedSite] = []
    sites, reports, sources, selected = _collect(config, files, stats, selected, dsn, coverage, boundary)
    statements = sorted({site.sourcemap.sql for site in sites})
    stats.distinct_sql = len(statements)
    if statements:
        if dsn is None or not dsn.strip():
            msg = f"no database URL; pass --database-url or set ${config.database_url_env}"
            raise CheckError(msg)
        if selected is None:
            selected = backend_for(dsn).checker()
        urls = (dsn,) * jobs
        async with AsyncExitStack() as stack:
            if hooks.own is not None:
                urls = (await stack.enter_async_context(hooks.own(dsn)),)
            elif hooks.scratch is not None:
                # never more workers than statements, so a scratch that builds a database per worker builds no spare
                workers = min(jobs, len(statements))
                built = await stack.enter_async_context(hooks.scratch(dsn, workers))
                # one URL is shared by every worker, several are one per worker
                urls = built.urls * workers if len(built.urls) == 1 else built.urls[:workers]
                stats.scratch_seconds = built.seconds
            if hooks.gate is not None:
                stats.migrations = await hooks.gate(urls[0])
            outcomes = await describe_all(urls, statements, config.statement_timeout_ms, selected)
        for site in sites:
            # collection inferred parameters before a backend existed; bind before they are compared
            if site.inference is not None:
                site.inference.bind(selected)
            outcome = outcomes[site.sourcemap.sql]
            reports.extend(_evaluate(site, outcome, selected))
            if inspect and not isinstance(outcome, (DatabaseError, _Operational)):
                inspection.append(
                    _inspection(
                        site,
                        outcome,
                        selected,
                        tuple(dict.fromkeys(value for value in (dsn, *urls) if value)),
                        config.root,
                    )
                )
    stats.by_rule.update(report.diagnostic.rule for report in reports)
    return RunResult(reports=reports, sources=sources, statistics=stats, coverage=coverage, inspection=inspection)
