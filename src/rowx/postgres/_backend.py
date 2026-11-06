from typing import TYPE_CHECKING, Final, cast

if TYPE_CHECKING:
    from collections.abc import Iterable
    from os import PathLike

from rowx._backend import (
    AdminBackend,
    CheckerBackend,
    CloneDatabase,
    Connect,
    ConnectChecker,
    CreateDatabase,
    DatabaseExists,
    DescribeQuery,
    DropDatabase,
    EnsureTemplate,
    ListDatabases,
    LoadMigrator,
    MapColumns,
    MigrateBackend,
    OpenChecker,
    ParseDsn,
    PoolFactory,
    TargetOptions,
    TestingBackend,
    TypeEntry,
    WithDatabase,
)
from rowx.postgres._admin import (
    create_database,
    database_exists,
    drop_database,
    list_databases,
    with_database,
)
from rowx.postgres._connection import Connection, connect
from rowx.postgres._core import connect_checker, describe, map_columns, parse_dsn, type_table
from rowx.postgres._migrate import DEFAULT_TABLE, Migrator
from rowx.postgres._options import ConnectOptions
from rowx.postgres._pool import Pool
from rowx.postgres._testing import clone, ensure_template

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


def load_migrator(
    path: str | PathLike[str],
    /,
    *,
    table: str = DEFAULT_TABLE,
    create_schemas: Iterable[str] = (),
    allow_out_of_order: bool = False,
    ignore_missing: bool = False,
) -> Migrator:
    return Migrator(
        path,
        table=table,
        create_schemas=create_schemas,
        allow_out_of_order=allow_out_of_order,
        ignore_missing=ignore_missing,
    )


async def open_checker(dsn: str, timeout_s: float) -> Connection:
    return Connection(await connect_checker(dsn, timeout_s))


def pool(dsn: str) -> Pool:
    return Pool(dsn)


def load() -> CheckerBackend:
    return CheckerBackend(
        connect_checker=cast("ConnectChecker", connect_checker),
        describe=cast("DescribeQuery", describe),
        parse_dsn=cast("ParseDsn", parse_dsn),
        map_columns=cast("MapColumns", map_columns),
        types=_types(),
        param_classes=VALUE_CLASSES,
        value_leaves=VALUE_LEAVES,
        range_leaves=RANGE_LEAVES,
        range_classes=RANGE_CLASSES,
        value_classes=VALUE_CLASSES,
        admin=AdminBackend(
            database_exists=cast("DatabaseExists", database_exists),
            create_database=cast("CreateDatabase", create_database),
            drop_database=cast("DropDatabase", drop_database),
            list_databases=cast("ListDatabases", list_databases),
            with_database=cast("WithDatabase", with_database),
        ),
        migrate=MigrateBackend(
            load_migrator=cast("LoadMigrator", load_migrator),
            connect=cast("Connect", connect),
            open_checker=cast("OpenChecker", open_checker),
            options=cast("TargetOptions", ConnectOptions.from_dsn),
        ),
        testing=TestingBackend(
            ensure_template=cast("EnsureTemplate", ensure_template),
            clone=cast("CloneDatabase", clone),
            pool=cast("PoolFactory", pool),
        ),
    )
