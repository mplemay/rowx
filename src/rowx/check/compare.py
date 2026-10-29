from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from rowx.postgres._types import accepts, by_key, result_type, result_types
from rowx.postgres._vocab import NullReasonName, TypeKey

if TYPE_CHECKING:
    from collections.abc import Sequence

    from rowx.check.model import Rule
    from rowx.check.params import ParamType
    from rowx.check.resolve import Elem, RowType

JSON_KEYS: Final[frozenset[TypeKey]] = frozenset({"json", "jsonb"})
USE_EXECUTE: Final[str] = "use .execute()"
type _Reason = NullReasonName | Literal[""]


# `type_key` is None for a type outside the table; an array column carries its innermost
# element key because the key alone is just "array"
@dataclass(slots=True, kw_only=True, frozen=True)
class ColumnDesc:
    name: str
    type_name: str
    type_key: TypeKey | None
    nullable: bool
    reason: _Reason = ""
    reason_detail: str | None = None
    element_key: TypeKey | None = None


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


def _label(column: ColumnDesc, index: int) -> str:
    return f'column {index + 1} "{column.name}"'


def _expected(column: ColumnDesc) -> str:
    if column.type_key == "array":
        leaf = result_type(column.element_key) if column.element_key is not None else "?"
        return f"list[{leaf}]"
    return "None" if column.type_key == "void" else result_type(column.type_key or "")


def _shown(elem: Elem) -> str:
    match elem.leaf:
        case "none":
            return "None"
        case "any":
            return "Any"
        case "list":
            return f"list[{_shown(elem.item)}]" if elem.item is not None else "list"
    return elem.leaf


def _array_matches(column: ColumnDesc, elem: Elem) -> bool:
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
    return item.leaf == "any" or column.element_key in JSON_KEYS or item.leaf in result_types(column.element_key)


# a range column's help names its subtype, since `Range[T]` is checked against it
def _range_help(column: ColumnDesc) -> str | None:
    key = column.element_key if column.type_key == "array" else column.type_key
    if key is None or by_key(key).matcher != "range":
        return None
    leaf = result_type(key)
    name = column.type_name.removeprefix("_") if column.type_key == "array" else column.type_name
    return f"`{name}` is a range of `{leaf[leaf.index('[') + 1 : -1]}`, so annotate it `{leaf}`"


def _leaf_matches(column: ColumnDesc, elem: Elem) -> bool:
    if elem.leaf == "any":
        return True
    # a bare `None` only ever matches `void`, so no wider rule below may accept it
    if elem.leaf == "none" or column.type_key == "void":
        return elem.leaf == "none" and column.type_key == "void"
    match column.type_key:
        case None:
            return False
        case "array":
            return _array_matches(column, elem)
        case key if key in JSON_KEYS:
            return True
        case key:
            return elem.leaf in result_types(key)


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


def _nullability_finding(column: ColumnDesc, index: int, elem: Elem, element: int | None) -> list[Finding]:
    if column.nullable == elem.nullable:
        return []
    if column.nullable:
        message = f"{_label(column, index)} may be NULL but the element is `{_shown(elem)}`"
        help_ = f'annotate `{_shown(elem)} | None`, or alias the column as "{column.name}!" if it can\'t be NULL'
    else:
        message = f"{_label(column, index)} is NOT NULL but the element is `{_shown(elem)} | None`"
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
        ),
    ]


def _compare_element(column: ColumnDesc, index: int, elem: Elem, element: int | None) -> list[Finding]:
    if not _supported(column):
        return [_unsupported(column, index, element)]
    if column.type_key == "void":
        return _void_finding(column, index, elem, element)
    # `Any` and `object` are the only annotations neutral to both type and nullability
    if elem.leaf == "any":
        return []
    if not _leaf_matches(column, elem):
        message = (
            f"{_label(column, index)}: column type is `{_expected(column)}` but the annotation is `{_shown(elem)}`"
        )
        return [Finding(rule="type-mismatch", message=message, column=index, element=element, help=_range_help(column))]
    return _nullability_finding(column, index, elem, element)


def compare(describe: DescribeDesc, row_type: RowType) -> list[Finding]:
    columns: Sequence[ColumnDesc] = describe.columns
    match row_type.kind:
        case "none":
            return []
        case "scalar":
            if len(columns) != 1:
                message = f"a scalar row type needs exactly 1 column but the query returns {len(columns)}"
                return [Finding(rule="arity-mismatch", message=message)]
            return _compare_element(columns[0], 0, row_type.elements[0], None)
        case "tuple":
            if len(columns) != len(row_type.elements):
                message = (
                    f"the tuple has {len(row_type.elements)} elements but the query returns {len(columns)} columns"
                )
                return [Finding(rule="arity-mismatch", message=message)]
            findings: list[Finding] = []
            for index, (column, elem) in enumerate(zip(columns, row_type.elements, strict=True)):
                findings.extend(_compare_element(column, index, elem, index))
            return findings


def _shown_param(evident: ParamType) -> str:
    return (
        f"list[{_shown_param(evident.item)}]" if evident.name == "list" and evident.item is not None else evident.name
    )


# `None` binds NULL for every parameter, and an unknown or unsupported parameter is never reported here
def _array_leaf(evident: ParamType) -> ParamType | None:
    # the dimension count of a parameter is unknown, so nested literals are flattened to their leaf
    item = evident
    while item.name == "list":
        if item.item is None:
            return None
        item = item.item
    return item


def _param_finding(index: int, type_name: str, key: TypeKey, evident: ParamType, noun: str) -> Finding:
    accepted = ", ".join(f"`{name}`" for name in by_key(key).accepted_params)
    message = (
        f'parameter ${index + 1} has type "{type_name}", {noun} accepts {accepted}, '
        f"but the interpolation is `{_shown_param(evident)}`"
    )
    return Finding(rule="param-type-mismatch", message=message)


def _element_finding(index: int, type_name: str, element_key: TypeKey, evident: ParamType) -> Finding | None:
    leaf = _array_leaf(evident)
    if leaf is None or leaf.name == "None" or accepts(leaf.name, element_key):
        return None
    return _param_finding(index, type_name, element_key, evident, "whose element")


def compare_param(
    index: int,
    type_name: str,
    type_key: TypeKey | None,
    evident: ParamType | None,
    element_key: TypeKey | None = None,
) -> Finding | None:
    if evident is None or type_key is None or evident.name == "None":
        return None
    if not accepts(evident.name, type_key):
        return _param_finding(index, type_name, type_key, evident, "which")
    if type_key == "array" and element_key is not None:
        return _element_finding(index, type_name, element_key, evident)
    return None
