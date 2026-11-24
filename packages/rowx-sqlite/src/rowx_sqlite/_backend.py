from typing import Final

from rowx_core.backend import Backend, CheckerBackend, TypeEntry

from rowx_sqlite._admin import SqliteAdmin
from rowx_sqlite._checker import SQL_ERROR_HELP
from rowx_sqlite._compiler import COMPILER
from rowx_sqlite._core import map_columns, type_table
from rowx_sqlite._scratch import SqliteScratch
from rowx_sqlite._services import SqliteMigrations, in_memory, open_checker
from rowx_sqlite._testing import provisioner

# the type keys that take any Python value; `any` is not one, since it decodes to one of the four storage classes
JSON_KEYS: Final[frozenset[str]] = frozenset({"json"})
# a column with no usable declared type decodes each value to its storage class
ANY_KEY: Final[str] = "any"
ANY_RESULTS: Final[frozenset[str]] = frozenset({"int", "float", "str", "bytes"})


def placeholder(number: int) -> str:
    return f"?{number}"


def cast(expression: str) -> str:
    return f"CAST({expression} AS TEXT)"


def _types() -> dict[str, TypeEntry]:
    return {
        info.key: TypeEntry(
            matcher="builtin",
            py_result=info.py_result,
            alternatives=ANY_RESULTS if info.key == ANY_KEY else frozenset(),
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
        param_classes={},
        value_leaves={},
        range_leaves={},
        range_classes=frozenset(),
        value_classes={},
        sql_error_help=SQL_ERROR_HELP,
        # SQLite has no identifier length limit, so an alias is never truncated
        override_issues=None,
        json_keys=JSON_KEYS,
        array_key=None,
        placeholder=placeholder,
        describer="SQLite",
        cast=cast,
        untyped_params=True,
        untyped_columns=True,
    )


def load() -> Backend:
    return Backend(
        checker_factory=checker,
        admin_factory=SqliteAdmin,
        migration_factory=SqliteMigrations,
        scratch_factory=SqliteScratch,
        testing_factory=provisioner,
        memory_factory=in_memory,
    )
