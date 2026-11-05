import asyncio
import os
from decimal import Decimal
from importlib import import_module
from pathlib import Path
from shutil import copytree
from subprocess import run  # ruff: ignore[suspicious-subprocess-import]
from sys import executable, modules
from typing import TYPE_CHECKING, Final

import pytest

from rowx import query_unchecked
from rowx.check.cli import main
from rowx.postgres import connect

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import ModuleType

pytestmark = [pytest.mark.integration, pytest.mark.db]

EXAMPLE: Final[Path] = Path(__file__).resolve().parents[5] / "examples" / "security"
NAMES: Final[list[str]] = ["Anvil", "Birdseed", "Catapult", "Dynamite", "Earmuffs"]


# the example is a directory of scripts, so its module is imported by its own name for the length of a test
@pytest.fixture
def catalog(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    monkeypatch.syspath_prepend(str(EXAMPLE))
    yield import_module("catalog")
    monkeypatch.delitem(modules, "catalog")


def names(rows: list[tuple[int, str, str, Decimal, int]]) -> list[str]:
    return [name for _, name, _, _, _ in rows]


def test_products_by_id_binds_a_list(catalog: ModuleType, security_schema: str) -> None:
    async def run_it() -> tuple[list[str], list[str]]:
        async with await connect(security_schema) as conn:
            return names(await catalog.products_by_id(conn, [3, 1])), names(await catalog.products_by_id(conn, []))

    assert asyncio.run(run_it()) == (["Anvil", "Catapult"], [])


def test_search_treats_none_as_no_filter(catalog: ModuleType, security_schema: str) -> None:
    async def run_it() -> list[list[str]]:
        async with await connect(security_schema) as conn:
            return [
                names(await catalog.search(conn)),
                names(await catalog.search(conn, category="hardware")),
                names(await catalog.search(conn, max_price=Decimal(20))),
                names(await catalog.search(conn, category="hardware", max_price=Decimal(20))),
            ]

    assert asyncio.run(run_it()) == [
        NAMES,
        ["Anvil", "Catapult", "Dynamite"],
        ["Birdseed", "Dynamite", "Earmuffs"],
        ["Dynamite"],
    ]


def test_listing_sorts_by_an_allowlisted_column_in_either_direction(catalog: ModuleType, security_schema: str) -> None:
    async def run_it() -> list[list[str]]:
        async with await connect(security_schema) as conn:
            return [
                names(await catalog.listing(conn, sort=catalog.parse_sort("price"))),
                names(await catalog.listing(conn, sort=catalog.parse_sort("price"), direction="desc")),
                names(await catalog.listing(conn, sort=catalog.SortColumn.STOCK, category="hardware")),
            ]

    assert asyncio.run(run_it()) == [
        ["Birdseed", "Earmuffs", "Dynamite", "Anvil", "Catapult"],
        ["Catapult", "Anvil", "Dynamite", "Earmuffs", "Birdseed"],
        ["Catapult", "Anvil", "Dynamite"],
    ]


def test_an_unlisted_column_or_direction_is_rejected_before_any_sql(catalog: ModuleType) -> None:
    for raw in ("internal_cost", "price; DROP TABLE products", "PRICE", ""):
        with pytest.raises(ValueError, match="is not a valid SortColumn"):
            catalog.parse_sort(raw)
    for raw in ("ASC", "sideways", ""):
        with pytest.raises(ValueError, match="unknown sort direction"):
            catalog.parse_direction(raw)
    assert catalog.parse_direction("desc") == "desc"


def test_a_column_that_is_not_selected_still_leaks_through_its_order(catalog: ModuleType, security_schema: str) -> None:
    # rowx quotes any identifier safely, so the allowlist is the only thing that keeps `internal_cost` out of reach
    async def run_it() -> None:
        async with await connect(security_schema) as conn:
            by_price = [row[0] for row in await catalog.listing(conn, sort="price")]
            by_cost = [row[0] for row in await catalog.listing(conn, sort="internal_cost")]
            assert by_price != by_cost
            # the rows hold no cost, but their order says Birdseed is the cheapest to make, then Dynamite
            assert by_cost[:2] == [2, 4]

    asyncio.run(run_it())


def test_stock_by_groups_by_a_literal_column(catalog: ModuleType, security_schema: str) -> None:
    async def run_it() -> None:
        async with await connect(security_schema) as conn:
            by_category = await catalog.stock_by(conn, "category")
            by_vendor = await catalog.stock_by(conn, "vendor")
            assert by_category == [("apparel", 15), ("garden", 120), ("hardware", 47)]
            assert by_vendor == [("Acme", 22), ("Roadrunner", 40), (None, 120)]

    asyncio.run(run_it())


def test_a_hostile_value_stays_a_value(catalog: ModuleType, security_schema: str) -> None:
    async def run_it() -> tuple[list[str], int]:
        async with await connect(security_schema) as conn:
            found = names(await catalog.search(conn, category="hardware' OR '1'='1"))
            count = await query_unchecked[int](t'SELECT count(*)::int4 AS "n!" FROM products').one(conn)
            return found, count

    assert asyncio.run(run_it()) == ([], 5)


def test_main_runs(security_schema: str) -> None:
    result = run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [executable, str(EXAMPLE / "catalog.py")],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "DATABASE_URL": security_schema},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[0] == "Catapult: $129.99, 2 in stock"
    assert "hardware: 47" in result.stdout
    assert "internal_cost is not a sort column" in result.stdout


def test_main_requires_a_database_url() -> None:
    env = {key: value for key, value in os.environ.items() if key != "DATABASE_URL"}
    result = run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [executable, str(EXAMPLE / "catalog.py")], check=False, capture_output=True, text=True, env=env
    )
    assert result.returncode != 0
    assert "DATABASE_URL is not set" in result.stderr


def test_check_passes(security_schema: str, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["check", str(EXAMPLE), "--database-url", security_schema])
    captured = capsys.readouterr()
    assert (code, captured.out, captured.err) == (0, "", "")


def test_check_reports_a_hand_built_template(
    security_schema: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    copy = tmp_path / "security"
    copytree(EXAMPLE, copy)
    # the rule runs on every file under the include roots, not only on files with queries
    (copy / "raw.py").write_text(
        "from string.templatelib import Template\n\n\ndef raw(user_input: str) -> Template:\n    return Template(user_input)\n"
    )
    code = main(["check", str(copy), "--database-url", security_schema])
    out = capsys.readouterr().out
    assert code == 1
    assert "error[template-construction]" in out
    assert "raw.py" in out


def test_check_reports_a_runtime_format_spec(
    security_schema: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    copy = tmp_path / "security"
    copytree(EXAMPLE, copy)
    (copy / "spec.py").write_text(
        'from rowx import query_unchecked\n\n\ndef spec(x: str, how: str):\n    return query_unchecked(t"SELECT {x:{how}}")\n'
    )
    code = main(["check", str(copy), "--database-url", security_schema])
    out = capsys.readouterr().out
    assert code == 1
    assert "error[dynamic-query]" in out


def test_check_describes_the_static_queries_and_skips_the_dynamic_ones(
    security_schema: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    copy = tmp_path / "security"
    copytree(EXAMPLE, copy)
    source = (copy / "catalog.py").read_text()
    old = "type ProductRow = tuple[int, str, str, Decimal, int]"
    assert old in source
    (copy / "catalog.py").write_text(source.replace(old, "type ProductRow = tuple[int, str, str, Decimal, str]"))
    code = main(["check", str(copy), "--database-url", security_schema])
    out = capsys.readouterr().out
    assert code == 1
    # one diagnostic for each of the two `query` calls, none for the `query_unchecked` ones
    assert out.count("error[type-mismatch]") == 2
