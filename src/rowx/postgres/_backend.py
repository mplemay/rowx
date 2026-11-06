from typing import Final, cast

from rowx._backend import (
    CheckerBackend,
    ConnectChecker,
    DescribeQuery,
    MapColumns,
    ParseDsn,
    TypeEntry,
)
from rowx.postgres._core import connect_checker, describe, map_columns, parse_dsn, type_table

PARAM_CLASSES: Final[dict[str, str]] = {
    "rowx.postgres.Range": "Range",
    "rowx.postgres._range.Range": "Range",
    "rowx.postgres.Money": "Money",
    "rowx.postgres._money.Money": "Money",
    "rowx.postgres.Interval": "Interval",
    "rowx.postgres._interval.Interval": "Interval",
    "rowx.postgres.Point": "Point",
    "rowx.postgres._geometric.Point": "Point",
    "rowx.postgres.Line": "Line",
    "rowx.postgres._geometric.Line": "Line",
    "rowx.postgres.LineSegment": "LineSegment",
    "rowx.postgres._geometric.LineSegment": "LineSegment",
    "rowx.postgres.Box": "Box",
    "rowx.postgres._geometric.Box": "Box",
    "rowx.postgres.Path": "Path",
    "rowx.postgres._geometric.Path": "Path",
    "rowx.postgres.Polygon": "Polygon",
    "rowx.postgres._geometric.Polygon": "Polygon",
    "rowx.postgres.Circle": "Circle",
    "rowx.postgres._geometric.Circle": "Circle",
    "rowx.postgres.Cube": "Cube",
    "rowx.postgres._cube.Cube": "Cube",
}
VALUE_LEAVES: Final[dict[str, str]] = {
    "rowx.postgres.Money": "rowx.postgres.Money",
    "rowx.postgres._money.Money": "rowx.postgres.Money",
    "rowx.postgres.Interval": "rowx.postgres.Interval",
    "rowx.postgres._interval.Interval": "rowx.postgres.Interval",
    "rowx.postgres.Point": "rowx.postgres.Point",
    "rowx.postgres._geometric.Point": "rowx.postgres.Point",
    "rowx.postgres.Line": "rowx.postgres.Line",
    "rowx.postgres._geometric.Line": "rowx.postgres.Line",
    "rowx.postgres.LineSegment": "rowx.postgres.LineSegment",
    "rowx.postgres._geometric.LineSegment": "rowx.postgres.LineSegment",
    "rowx.postgres.Box": "rowx.postgres.Box",
    "rowx.postgres._geometric.Box": "rowx.postgres.Box",
    "rowx.postgres.Path": "rowx.postgres.Path",
    "rowx.postgres._geometric.Path": "rowx.postgres.Path",
    "rowx.postgres.Polygon": "rowx.postgres.Polygon",
    "rowx.postgres._geometric.Polygon": "rowx.postgres.Polygon",
    "rowx.postgres.Circle": "rowx.postgres.Circle",
    "rowx.postgres._geometric.Circle": "rowx.postgres.Circle",
    "rowx.postgres.Cube": "rowx.postgres.Cube",
    "rowx.postgres._cube.Cube": "rowx.postgres.Cube",
}
RANGE_LEAVES: Final[dict[str, str]] = {
    "int": "rowx.postgres.Range[int]",
    "decimal.Decimal": "rowx.postgres.Range[decimal.Decimal]",
    "datetime.date": "rowx.postgres.Range[datetime.date]",
    "datetime.datetime": "rowx.postgres.Range[datetime.datetime]",
}
RANGE_CLASSES: Final[frozenset[str]] = frozenset({"rowx.postgres.Range", "rowx.postgres._range.Range"})
VALUE_CLASSES: Final[dict[str, str]] = {
    qualname: qualname.rsplit(".", 1)[-1] for qualname in (*VALUE_LEAVES, *RANGE_CLASSES)
}


def _types() -> dict[str, TypeEntry]:
    return {
        info.key: TypeEntry(
            matcher=info.matcher,
            py_result=info.py_result,
            alternatives=frozenset(info.alt_py_results),
            accepted_params=tuple(info.accepted_params),
        )
        for info in type_table()
    }


def load() -> CheckerBackend:
    return CheckerBackend(
        connect_checker=cast("ConnectChecker", connect_checker),
        describe=cast("DescribeQuery", describe),
        parse_dsn=cast("ParseDsn", parse_dsn),
        map_columns=cast("MapColumns", map_columns),
        types=_types(),
        param_classes=PARAM_CLASSES,
        value_leaves=VALUE_LEAVES,
        range_leaves=RANGE_LEAVES,
        range_classes=RANGE_CLASSES,
        value_classes=VALUE_CLASSES,
    )
