from dataclasses import dataclass, replace
from re import Pattern, compile as re_compile
from typing import TYPE_CHECKING, Final, Literal

from rowx_core.backend import CheckerBackend, NullReason, ShapeIssue

if TYPE_CHECKING:
    from collections.abc import Sequence

    from rowx.check.model import Rule
    from rowx.check.params import ParamType
    from rowx.check.resolve import Elem, NamedField, RowType

JSON_KEYS: Final[frozenset[str]] = frozenset({"json", "jsonb"})
USE_EXECUTE: Final[str] = "use .execute()"
UNNAMED: Final[str] = "?column?"
LOWERCASE_IDENTIFIER: Final[Pattern[str]] = re_compile(r"[a-z_][a-z0-9_]*")
type _Reason = NullReason | Literal[""]


# `type_key` is None for a type outside the table; an array column carries its innermost
# element key because the key alone is just "array"
@dataclass(slots=True, kw_only=True, frozen=True)
class ColumnDesc:
    name: str
    type_name: str
    type_key: str | None
    nullable: bool
    reason: _Reason = ""
    reason_detail: str | None = None
    element_key: str | None = None
    # the name as the server sent it, with the `!`/`?` suffix; it is what `map_columns` strips, once
    raw_name: str | None = None


@dataclass(slots=True, kw_only=True, frozen=True)
class DescribeDesc:
    columns: tuple[ColumnDesc, ...]


# `column` and `element` are 0-based; `element` is None for a finding about the whole row type
@dataclass(slots=True, kw_only=True, frozen=True)
class Finding:
    rule: Rule
    message: str
    column: int | None = None
    element: int | None = None
    help: str | None = None
    reason: _Reason = ""
    reason_detail: str | None = None
    # the indices into a named row's fields that the finding concerns, for the field notes and the JSON `field`
    fields: tuple[int, ...] = ()
    notes: tuple[str, ...] = ()
    wider_nullability: bool = False


def _rules(backend: CheckerBackend | None) -> CheckerBackend:
    if backend is None:
        msg = "checker rules require an explicit backend"
        raise ValueError(msg)
    return backend


def _py_result(rules: CheckerBackend, key: str) -> str:
    return rules.types[key].py_result


def _py_results(rules: CheckerBackend, key: str) -> frozenset[str]:
    entry = rules.types[key]
    return frozenset((entry.py_result, *entry.alternatives))


def _accepts(rules: CheckerBackend, py_type_name: str, key: str) -> bool:
    accepted = rules.types[key].accepted_params
    # `object` (json/jsonb) takes any value; json.dumps is the arbiter at runtime
    return "object" in accepted or py_type_name in accepted


def _label(column: ColumnDesc, index: int) -> str:
    return f'column {index + 1} "{column.name}"'


def expected_type(column: ColumnDesc, rules: CheckerBackend) -> str:
    if column.type_key == "array":
        leaf = _py_result(rules, column.element_key) if column.element_key is not None else "?"
        return f"list[{leaf}]"
    return "None" if column.type_key == "void" else _py_result(rules, column.type_key or "")


def _shown(elem: Elem) -> str:
    match elem.leaf:
        case "none":
            return "None"
        case "any":
            return "Any"
        case "list":
            return f"list[{_shown(elem.item)}]" if elem.item is not None else "list"
    return elem.leaf


def _array_matches(column: ColumnDesc, elem: Elem, rules: CheckerBackend) -> bool:
    if column.element_key is None:
        return False
    # the declared type does not record dimensions, so any nesting depth is accepted (#112); an element at any
    # level may be `X` or `X | None`
    if elem.leaf != "list":
        return False
    item = elem.item
    while item is not None and item.leaf == "list":
        item = item.item
    if item is None:
        return False
    return item.leaf == "any" or column.element_key in JSON_KEYS or item.leaf in _py_results(rules, column.element_key)


# a range column's help names its subtype, since `Range[T]` is checked against it
def _range_help(column: ColumnDesc, rules: CheckerBackend) -> str | None:
    key = column.element_key if column.type_key == "array" else column.type_key
    if key is None or rules.types[key].matcher != "range":
        return None
    leaf = _py_result(rules, key)
    name = column.type_name.removeprefix("_") if column.type_key == "array" else column.type_name
    return f"`{name}` is a range of `{leaf[leaf.index('[') + 1 : -1]}`, so annotate it `{leaf}`"


def _leaf_matches(column: ColumnDesc, elem: Elem, rules: CheckerBackend) -> bool:
    if elem.leaf == "any":
        return True
    # a bare `None` only ever matches `void`, so no wider rule below may accept it
    if elem.leaf == "none" or column.type_key == "void":
        return elem.leaf == "none" and column.type_key == "void"
    match column.type_key:
        case None:
            return False
        case "array":
            return _array_matches(column, elem, rules)
        case key if key in JSON_KEYS:
            return True
        case key:
            return elem.leaf in _py_results(rules, key)


def _unsupported(column: ColumnDesc, index: int, element: int | None) -> Finding:
    return Finding(
        rule="unsupported-type",
        message=f'{_label(column, index)} has unsupported type "{column.type_name}"',
        column=index,
        element=element,
        help="cast the column to a supported type, such as `::text`",
    )


def _supported(column: ColumnDesc) -> bool:
    if column.type_key is None:
        return False
    if column.type_key == "array":
        return column.element_key is not None
    return True


def _void_finding(column: ColumnDesc, index: int, elem: Elem, element: int | None) -> list[Finding]:
    if elem.leaf == "none":
        return []
    message = f"{_label(column, index)}: `void` column faces `{_shown(elem)}`, which only matches the element `None`"
    return [Finding(rule="type-mismatch", message=message, column=index, element=element, help=USE_EXECUTE)]


def _nullability_finding(
    column: ColumnDesc, index: int, elem: Elem, element: int | None, subject: str | None
) -> list[Finding]:
    if column.nullable == elem.nullable:
        return []
    if column.nullable:
        message = f"{_label(column, index)} may be NULL but {subject or 'the element'} is `{_shown(elem)}`"
        help_ = f'annotate `{_shown(elem)} | None`, or alias the column as "{column.name}!" if it can\'t be NULL'
    else:
        message = f"{_label(column, index)} is NOT NULL but {subject or 'the element'} is `{_shown(elem)} | None`"
        help_ = f'annotate `{_shown(elem)}`, or alias the column as "{column.name}?" if it may be NULL'
    return [
        Finding(
            rule="nullability-mismatch",
            message=message,
            column=index,
            element=element,
            help=help_,
            reason=column.reason,
            reason_detail=column.reason_detail,
            wider_nullability=not column.nullable,
        ),
    ]


# `subject` is the field of a named row, such as `field "age" of `User``; a tuple element has none
def _compare_element(  # ruff: ignore[too-many-arguments, too-many-positional-arguments]
    column: ColumnDesc,
    index: int,
    elem: Elem,
    element: int | None,
    rules: CheckerBackend,
    subject: str | None = None,
) -> list[Finding]:
    if not _supported(column):
        return [_unsupported(column, index, element)]
    if column.type_key == "void":
        return _void_finding(column, index, elem, element)
    # `Any` and `object` are the only annotations neutral to both type and nullability
    if elem.leaf == "any":
        return []
    if not _leaf_matches(column, elem, rules):
        if subject is not None:
            message = f"{_label(column, index)} is `{expected_type(column, rules)}` but {subject} is `{_shown(elem)}`"
        else:
            expected = expected_type(column, rules)
            message = f"{_label(column, index)}: column type is `{expected}` but the annotation is `{_shown(elem)}`"
        return [
            Finding(
                rule="type-mismatch",
                message=message,
                column=index,
                element=element,
                help=_range_help(column, rules),
            ),
        ]
    return _nullability_finding(column, index, elem, element, subject)


def _alias_for(name: str) -> str:
    return f"AS {name}" if LOWERCASE_IDENTIFIER.fullmatch(name) else f'AS "{name}"'


def _join(items: list[str]) -> str:
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _issue_finding(issue: ShapeIssue, columns: Sequence[ColumnDesc], row_type: RowType) -> Finding:
    type_name = row_type.type_name
    fields = row_type.fields
    positions = issue.columns
    first = positions[0] if positions else None
    name = columns[first].name if first is not None else ""
    match issue.kind:
        case "unexpected-column":
            help_ = (
                "alias the expression"
                if name == UNNAMED
                else f'add a field named "{name}" to `{type_name}`, or remove the column from the SELECT'
            )
            message = f'column {(first or 0) + 1} "{name}" matches no field of `{type_name}`'
            return Finding(rule="unexpected-column", message=message, column=first, help=help_)
        case "missing-column":
            field_ = fields[issue.fields[0]]
            how = "mark the key `NotRequired`" if row_type.form == "dict" else "give the field a default"
            help_ = f'select a column named "{field_.key}", alias one with {_alias_for(field_.key)}, or {how}'
            message = f'field "{field_.key}" of `{type_name}` has no column'
            return Finding(rule="missing-column", message=message, help=help_, fields=(issue.fields[0],))
        case "duplicate-column":
            return _duplicate_finding(issue, columns, row_type)
        case "ambiguous-column":
            keys = _join([f'"{fields[index].key}"' for index in issue.fields])
            message = f'column {(first or 0) + 1} "{name}" matches fields {keys} of `{type_name}`'
            help_ = "give the fields distinct aliases, or remove populate_by_name if it causes the overlap"
            return Finding(
                rule="ambiguous-column",
                message=message,
                column=first,
                help=help_,
                fields=tuple(issue.fields),
            )
        case "column-name-mismatch":
            field_ = fields[issue.fields[0]]
            message = (
                f'column {(first or 0) + 1} "{name}" faces field "{field_.key}" of `{type_name}` '
                f"(position {(first or 0) + 1})"
            )
            help_ = f"reorder the SELECT, alias the column {_alias_for(field_.key)}, or rename the field"
            return Finding(
                rule="column-name-mismatch",
                message=message,
                column=first,
                help=help_,
                fields=(issue.fields[0],),
            )
    low, high, actual = issue.min or 0, issue.max or 0, issue.actual or 0
    takes = f"exactly {low}" if low == high else f"between {low} and {high}"
    message = f"the query returns {actual} columns but `{type_name}` takes {takes}"
    help_ = f"select exactly {low} columns" if low == high else f"select between {low} and {high} columns"
    return Finding(rule="arity-mismatch", message=message, help=help_)


def _duplicate_finding(issue: ShapeIssue, columns: Sequence[ColumnDesc], row_type: RowType) -> Finding:
    type_name = row_type.type_name
    positions = issue.columns
    names = [columns[index].name for index in positions]
    verb = "both" if len(positions) == len(("a", "b")) else "all"
    # the second occurrence is the one that is reported
    reported = positions[1]
    if issue.name is not None:
        numbers = _join([str(index + 1) for index in positions])
        if issue.name == UNNAMED:
            message = f"columns {numbers} are {verb} unnamed (`{UNNAMED}`)"
            return Finding(rule="duplicate-column", message=message, column=reported, help="alias the expression")
        message = f'columns {numbers} are {verb} named "{issue.name}"'
        return Finding(
            rule="duplicate-column", message=message, column=reported, help="alias one of the columns with AS"
        )
    # two accepted names of one field
    field_ = row_type.fields[issue.fields[0]]
    listed = _join([f'{index + 1} "{name}"' for index, name in zip(positions, names, strict=True)])
    message = f'columns {listed} {verb} map to field "{field_.key}" of `{type_name}`'
    help_ = f"select only one of {_join([f'"{name}"' for name in names])}"
    return Finding(rule="duplicate-column", message=message, column=reported, help=help_, fields=(issue.fields[0],))


def _order(finding: Finding) -> tuple[int, int, int]:
    # a finding about the whole row first, then the column-indexed ones by column, then `missing-column` by field
    if finding.column is not None:
        return (1, finding.column, 0 if finding.rule not in {"type-mismatch", "nullability-mismatch"} else 1)
    if finding.rule == "missing-column":
        return (2, finding.fields[0], 0)
    return (0, 0, 0)


def _compare_named(columns: Sequence[ColumnDesc], row_type: RowType, rules: CheckerBackend) -> list[Finding]:
    fields: list[NamedField] = list(row_type.fields)
    positional = row_type.matching == "positional"
    raw = [column.name if column.raw_name is None else column.raw_name for column in columns]
    mapping, issues = rules.map_columns(raw, [(field_.names, field_.required) for field_ in fields], positional)
    findings = [_issue_finding(issue, columns, row_type) for issue in issues]
    # a positional row stops at its first error; a by-name row still checks every column that mapped to one field
    skipped = {
        index for issue in issues if issue.kind in {"duplicate-column", "ambiguous-column"} for index in issue.columns
    }
    if not (positional and issues):
        for index, (column, target) in enumerate(zip(columns, mapping, strict=True)):
            if target is None or index in skipped:
                continue
            field_ = fields[target]
            subject = f'field "{field_.key}" of `{row_type.type_name}`'
            for finding in _compare_element(column, index, field_.elem, None, rules, subject):
                if finding.rule == "unsupported-type":
                    findings.append(finding)
                else:
                    findings.append(replace(finding, fields=(target,)))
    # the by-name rules were skipped for fields with unknown names, which the diagnostics say
    reasons = list(
        dict.fromkeys(field_.reason for field_ in fields if field_.names is None and field_.reason is not None)
    )
    if reasons:
        note = f"names not checked: {'; '.join(reasons)}"
        findings = [replace(finding, notes=(*finding.notes, note)) for finding in findings]
    return sorted(findings, key=_order)


def compare(
    describe: DescribeDesc,
    row_type: RowType,
    backend: CheckerBackend | None = None,
) -> list[Finding]:
    rules = _rules(backend)
    columns: Sequence[ColumnDesc] = describe.columns
    match row_type.kind:
        case "none":
            return []
        case "scalar":
            if len(columns) != 1:
                message = f"a scalar row type needs exactly 1 column but the query returns {len(columns)}"
                return [Finding(rule="arity-mismatch", message=message)]
            return _compare_element(columns[0], 0, row_type.elements[0], None, rules)
        case "named":
            return _compare_named(columns, row_type, rules)
        case "tuple":
            if len(columns) != len(row_type.elements):
                message = (
                    f"the tuple has {len(row_type.elements)} elements but the query returns {len(columns)} columns"
                )
                return [Finding(rule="arity-mismatch", message=message)]
            findings: list[Finding] = []
            for index, (column, elem) in enumerate(zip(columns, row_type.elements, strict=True)):
                findings.extend(_compare_element(column, index, elem, index, rules))
            return findings


def shown_param(evident: ParamType) -> str:
    return f"list[{shown_param(evident.item)}]" if evident.name == "list" and evident.item is not None else evident.name


# `None` binds NULL for every parameter, and an unknown or unsupported parameter is never reported here
def _array_leaf(evident: ParamType) -> ParamType | None:
    # the dimension count of a parameter is unknown, so nested literals are flattened to their leaf
    item = evident
    while item.name == "list":
        if item.item is None:
            return None
        item = item.item
    return item


def _param_finding(  # ruff: ignore[too-many-arguments, too-many-positional-arguments]
    index: int,
    type_name: str,
    key: str,
    evident: ParamType,
    noun: str,
    rules: CheckerBackend,
) -> Finding:
    accepted = ", ".join(f"`{name}`" for name in rules.types[key].accepted_params)
    message = (
        f'parameter ${index + 1} has type "{type_name}", {noun} accepts {accepted}, '
        f"but the interpolation is `{shown_param(evident)}`"
    )
    return Finding(rule="param-type-mismatch", message=message)


def _element_finding(
    index: int,
    type_name: str,
    element_key: str,
    evident: ParamType,
    rules: CheckerBackend,
) -> Finding | None:
    leaf = _array_leaf(evident)
    if leaf is None or leaf.name == "None" or _accepts(rules, leaf.name, element_key):
        return None
    return _param_finding(index, type_name, element_key, evident, "whose element", rules)


def compare_param_result(  # ruff: ignore[too-many-arguments, too-many-positional-arguments]
    index: int,
    type_name: str,
    type_key: str | None,
    evident: ParamType | None,
    element_key: str | None = None,
    backend: CheckerBackend | None = None,
) -> tuple[Finding | None, bool]:
    if evident is None or type_key is None or evident.name == "None":
        return None, False
    rules = _rules(backend)
    if not _accepts(rules, evident.name, type_key):
        return _param_finding(index, type_name, type_key, evident, "which", rules), True
    if type_key == "array":
        leaf = _array_leaf(evident)
        if element_key is None or leaf is None or leaf.name == "None":
            return None, False
        return _element_finding(index, type_name, element_key, evident, rules), True
    return None, True


def compare_param(  # ruff: ignore[too-many-arguments, too-many-positional-arguments]
    index: int,
    type_name: str,
    type_key: str | None,
    evident: ParamType | None,
    element_key: str | None = None,
    backend: CheckerBackend | None = None,
) -> Finding | None:
    return compare_param_result(index, type_name, type_key, evident, element_key, backend)[0]
