from typing import Final

from rowx_core.backend import Backend, CheckerBackend, TypeEntry

from rowx_postgres._checker import SQL_ERROR_HELP, override_issues
from rowx_postgres._compiler import COMPILER
from rowx_postgres._core import map_columns, type_table
from rowx_postgres._embedded_testing import provisioner
from rowx_postgres._scratch import embedded_service
from rowx_postgres._services import PostgresAdmin, PostgresMigrations, open_checker

VALUE_LEAVES: Final[dict[str, str]] = {
    "rowx.postgres.Money": "rowx_postgres.Money",
    "rowx.postgres.Interval": "rowx_postgres.Interval",
    "rowx.postgres.Point": "rowx_postgres.Point",
    "rowx.postgres.Line": "rowx_postgres.Line",
    "rowx.postgres.LineSegment": "rowx_postgres.LineSegment",
    "rowx.postgres.Box": "rowx_postgres.Box",
    "rowx.postgres.Path": "rowx_postgres.Path",
    "rowx.postgres.Polygon": "rowx_postgres.Polygon",
    "rowx.postgres.Circle": "rowx_postgres.Circle",
    "rowx.postgres.Cube": "rowx_postgres.Cube",
    "rowx_postgres.Money": "rowx_postgres.Money",
    "rowx_postgres._money.Money": "rowx_postgres.Money",
    "rowx_postgres.Interval": "rowx_postgres.Interval",
    "rowx_postgres._interval.Interval": "rowx_postgres.Interval",
    "rowx_postgres.Point": "rowx_postgres.Point",
    "rowx_postgres._geometric.Point": "rowx_postgres.Point",
    "rowx_postgres.Line": "rowx_postgres.Line",
    "rowx_postgres._geometric.Line": "rowx_postgres.Line",
    "rowx_postgres.LineSegment": "rowx_postgres.LineSegment",
    "rowx_postgres._geometric.LineSegment": "rowx_postgres.LineSegment",
    "rowx_postgres.Box": "rowx_postgres.Box",
    "rowx_postgres._geometric.Box": "rowx_postgres.Box",
    "rowx_postgres.Path": "rowx_postgres.Path",
    "rowx_postgres._geometric.Path": "rowx_postgres.Path",
    "rowx_postgres.Polygon": "rowx_postgres.Polygon",
    "rowx_postgres._geometric.Polygon": "rowx_postgres.Polygon",
    "rowx_postgres.Circle": "rowx_postgres.Circle",
    "rowx_postgres._geometric.Circle": "rowx_postgres.Circle",
    "rowx_postgres.Cube": "rowx_postgres.Cube",
    "rowx_postgres._cube.Cube": "rowx_postgres.Cube",
}
RANGE_LEAVES: Final[dict[str, str]] = {
    "int": "rowx_postgres.Range[int]",
    "decimal.Decimal": "rowx_postgres.Range[decimal.Decimal]",
    "datetime.date": "rowx_postgres.Range[datetime.date]",
    "datetime.datetime": "rowx_postgres.Range[datetime.datetime]",
}
RANGE_CLASSES: Final[frozenset[str]] = frozenset(
    {"rowx.postgres.Range", "rowx_postgres.Range", "rowx_postgres._range.Range"},
)
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


def checker() -> CheckerBackend:
    return CheckerBackend(
        connect_checker=open_checker,
        compiler=COMPILER,
        map_columns=map_columns,
        types=_types(),
        param_classes=VALUE_CLASSES,
        value_leaves=VALUE_LEAVES,
        range_leaves=RANGE_LEAVES,
        range_classes=RANGE_CLASSES,
        value_classes=VALUE_CLASSES,
        sql_error_help=SQL_ERROR_HELP,
        override_issues=override_issues,
    )


def load() -> Backend:
    return Backend(
        checker_factory=checker,
        admin_factory=PostgresAdmin,
        migration_factory=PostgresMigrations,
        testing_factory=provisioner,
        embedded_factory=embedded_service,
    )
