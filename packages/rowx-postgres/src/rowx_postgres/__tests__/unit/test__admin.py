import ast
import asyncio
import inspect
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from rowx_postgres import ConnectOptions
from rowx_postgres._admin import with_database
from rowx_postgres._core import (
    DatabaseInfo,
    create_database,
    database_exists,
    drop_database,
    list_databases,
)

if TYPE_CHECKING:
    from collections.abc import Callable

CORE_STUB: Final[Path] = Path(__file__).resolve().parents[2] / "_core.pyi"
ADMIN_FUNCTIONS: Final[dict[str, Callable[..., object]]] = {
    "database_exists": database_exists,
    "create_database": create_database,
    "drop_database": drop_database,
    "list_databases": list_databases,
}


@pytest.mark.parametrize(
    ("dsn", "expected"),
    [
        ("postgresql://u:p@h:5432/app", "postgresql://u:p@h:5432/new?dbname=new"),
        ("postgres://u@h", "postgres://u@h/new?dbname=new"),
        ("postgresql://u@h/app?sslmode=disable&dbname=other", "postgresql://u@h/new?sslmode=disable&dbname=new"),
        ("postgresql://u@a:1,b:2/app?connect_timeout=3", "postgresql://u@a:1,b:2/new?connect_timeout=3&dbname=new"),
        ("host=h user=u dbname=app", "host=h user=u dbname=app dbname='new'"),
    ],
)
def test_with_database(dsn: str, expected: str) -> None:
    assert with_database(dsn, "new") == expected


# every spelling the driver reads as `dbname` must lose to the new name
@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://u@h/app?%64bname=production",
        "postgresql://u@h/app?%64%62%6E%61%6D%65=production&sslmode=disable",
        "postgresql://u@h/app?dbname=production&dbname=production",
        "postgresql://u@h?dbname=production",
        " postgresql://u@h/production ",
        "host=h dbname=production",
        "dbname='production' host=h",
    ],
)
def test_with_database_overrides_every_dbname(dsn: str) -> None:
    assert ConnectOptions.from_dsn(dsn).database == "production"
    url = with_database(dsn, "rowx_check_0123456789abcdef")
    assert ConnectOptions.from_dsn(url).database == "rowx_check_0123456789abcdef"


def test_with_database_keeps_everything_else() -> None:
    dsn = "postgresql://u:p%40ss@h:5433/app?sslmode=disable&application_name=a%20b&options=-c%20search_path%3Dx"
    before = ConnectOptions.from_dsn(dsn)
    after = ConnectOptions.from_dsn(with_database(dsn, "new"))
    assert after.database == "new"
    assert (after.host, after.port, after.user, after.password) == ("h", 5433, "u", "p@ss")
    assert (after.sslmode, after.application_name, after.server_settings) == (
        before.sslmode,
        before.application_name,
        before.server_settings,
    )


@pytest.mark.parametrize("name", ["odd name", "it's", "back\\slash", "ünï"])
def test_with_database_quotes_the_name(name: str) -> None:
    for dsn in ("postgresql://u@h/app", "host=h user=u"):
        assert ConnectOptions.from_dsn(with_database(dsn, name)).database == name


def literal(node: ast.expr) -> object:
    if isinstance(node, ast.Constant):
        return node.value
    msg = "the stub default is not a literal"
    raise AssertionError(msg)


def stub_parameters(node: ast.AsyncFunctionDef) -> list[tuple[str, str, object]]:
    empty = inspect.Parameter.empty
    positional = [*node.args.posonlyargs, *node.args.args]
    kinds = ["POSITIONAL_ONLY"] * len(node.args.posonlyargs)
    kinds += ["POSITIONAL_OR_KEYWORD"] * len(node.args.args)
    # defaults line up with the tail of the positional parameters
    missing = len(positional) - len(node.args.defaults)
    defaults: list[object] = [empty] * missing
    defaults.extend(literal(item) for item in node.args.defaults)
    found = list(zip((arg.arg for arg in positional), kinds, defaults, strict=True))
    if node.args.vararg is not None:
        found.append((node.args.vararg.arg, "VAR_POSITIONAL", empty))
    found.extend(
        (arg.arg, "KEYWORD_ONLY", empty if item is None else literal(item))
        for arg, item in zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True)
    )
    if node.args.kwarg is not None:
        found.append((node.args.kwarg.arg, "VAR_KEYWORD", empty))
    return found


def runtime_parameters(function: Callable[..., object]) -> list[tuple[str, str, object]]:
    return [(item.name, item.kind.name, item.default) for item in inspect.signature(function).parameters.values()]


def test_database_info_is_frozen_and_equal_by_value() -> None:
    left = DatabaseInfo(name="app", comment=None, sessions=0)
    right = DatabaseInfo(name="app", comment=None, sessions=0)
    other = DatabaseInfo(name="app", comment="note", sessions=1)
    with pytest.raises(TypeError):
        DatabaseInfo("app", None, 0)  # type: ignore[call-arg]  # ty: ignore[missing-argument, too-many-positional-arguments]
    with pytest.raises(AttributeError):
        left.name = "other"  # ty: ignore[invalid-assignment]
    assert left == right
    assert left is not right
    assert hash(left) == hash(right)
    assert left != other
    assert hash(left) != hash(other)
    assert DatabaseInfo.__module__ == "rowx_postgres._core"


def test_admin_signatures_match_the_stub() -> None:
    tree = ast.parse(CORE_STUB.read_text(encoding="utf-8"), filename=str(CORE_STUB))
    stubs = {
        node.name: node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name in ADMIN_FUNCTIONS
    }
    assert set(stubs) == set(ADMIN_FUNCTIONS)
    for name, function in ADMIN_FUNCTIONS.items():
        assert stub_parameters(stubs[name]) == runtime_parameters(function)
    returned = stubs["list_databases"].returns
    assert isinstance(returned, ast.Subscript)
    assert isinstance(returned.value, ast.Name)
    assert returned.value.id == "list"
    assert isinstance(returned.slice, ast.Name)
    assert returned.slice.id == "DatabaseInfo"


def test_keyword_only_admin_arguments_are_rejected_positionally() -> None:
    async def reject() -> None:
        with pytest.raises(TypeError):
            await create_database(
                "postgresql://u@127.0.0.1:1",
                "app",
                "template0",  # ty: ignore[too-many-positional-arguments]
            )
        with pytest.raises(TypeError):
            await drop_database(
                "postgresql://u@127.0.0.1:1",
                "app",
                True,  # ruff: ignore[boolean-positional-value-in-call]  # ty: ignore[too-many-positional-arguments]
            )

    asyncio.run(reject())
