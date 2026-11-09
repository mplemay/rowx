import subprocess  # ruff: ignore[suspicious-subprocess-import]
import sys
from os import environ
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

if TYPE_CHECKING:
    from rowx_core import Connection, Executor, Pool, Transaction
    from rowx_postgres import Connection as PostgresConnection, Pool as PostgresPool, Transaction as PostgresTransaction

    def contracts(
        conn: PostgresConnection, pool: PostgresPool, tx: PostgresTransaction
    ) -> tuple[Connection, Pool[PostgresConnection], Transaction, Executor]:
        return conn, pool, tx, pool

    async def portable(pool: Pool[Connection]) -> Connection:
        async with pool.acquire() as conn:
            async with conn.transaction() as tx:
                await tx.rollback()
            return conn


SOURCE: Final[Path] = Path(__file__).resolve()
ROOT: Final[Path] = next(parent for parent in SOURCE.parents if (parent / "Cargo.lock").is_file())
INVALID: Final[str] = """from rowx_core import Connection

def invalid(value: object) -> Connection:
    return value
"""
CONTRACTS: Final[str] = """from rowx_core import Connection, Executor, Pool, Transaction
from rowx.postgres import Connection as PostgresConnection, Pool as PostgresPool, Transaction as PostgresTransaction

def contracts(
    conn: PostgresConnection, pool: PostgresPool, tx: PostgresTransaction
) -> tuple[Connection, Pool[PostgresConnection], Transaction, Executor]:
    return conn, pool, tx, pool

async def portable(pool: Pool[Connection]) -> Connection:
    async with pool.acquire() as conn:
        async with conn.transaction() as tx:
            await tx.rollback()
        return conn
"""


def check(tool: str, source: Path) -> subprocess.CompletedProcess[str]:
    args = [sys.executable, "-m", tool]
    target_python = environ.get("ROWX_TYPING_PYTHON")
    if tool == "ty":
        args.extend(["check", "--python-version", "3.14"])
        if target_python is not None:
            args.extend(["--python", target_python])
    elif tool == "pyright":
        args.extend(["--pythonversion", "3.14"])
        if target_python is not None:
            args.extend(["--pythonpath", target_python])
    else:
        args.extend(["--python-version", "3.14", "--follow-imports", "silent"])
        if target_python is not None:
            args.extend(["--python-executable", target_python])
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [*args, str(source)],
        capture_output=True,
        text=True,
        check=False,
        cwd=source.parent if target_python is not None else ROOT,
    )


@pytest.mark.parametrize("tool", ["pyright", "mypy", "ty"])
def test_postgres_implements_shared_executor_contracts(tool: str, tmp_path: Path) -> None:
    positive_source = tmp_path / "contracts.py"
    positive_source.write_text(CONTRACTS)
    positive = check(tool, positive_source)
    assert positive.returncode == 0, positive.stdout + positive.stderr
    invalid = tmp_path / "invalid.py"
    invalid.write_text(INVALID)
    negative = check(tool, invalid)
    assert negative.returncode != 0, "type checker accepted an object as a Connection"
    assert "invalid.py" in negative.stdout
