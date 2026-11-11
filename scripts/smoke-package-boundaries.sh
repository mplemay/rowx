#!/usr/bin/env bash
# Exercise each supported package combination outside the editable checkout.
set -euo pipefail

if (($# != 1)); then
  echo "usage: $0 <distribution-directory>" >&2
  exit 2
fi

distributions="$(cd "$1" && pwd)"
rowx_wheels=("$distributions"/rowx-*.whl)
workspace="$(mktemp -d)"
trap 'rm -rf "$workspace"' EXIT
cd "$workspace"

uv venv --python "${PYTHON:-3.14}" core
uv pip install --refresh --python core/bin/python --no-index --find-links "$distributions" rowx-core
core/bin/python - <<'PY'
from importlib.util import find_spec
from rowx_core import query

assert find_spec("rowx") is None
assert find_spec("rowx_postgres") is None
assert query[int](t"SELECT {1}") is not None
PY

uv venv --python "${PYTHON:-3.14}" postgres
uv pip install --refresh --python postgres/bin/python --no-index --find-links "$distributions" rowx-postgres
postgres/bin/python - <<'PY'
import asyncio
from os import environ
from importlib.util import find_spec
from pathlib import Path
from secrets import token_hex
from tempfile import TemporaryDirectory
from rowx_core import InterfaceError, query, query_unchecked
from rowx_postgres import ConnectOptions, Migrator, connect
from rowx_postgres import _core
from rowx_postgres._compiler import COMPILER
from rowx_postgres._testing import ensure_template

assert find_spec("rowx") is None
assert query[int](t"SELECT {1}") is not None
assert ConnectOptions.from_dsn("postgresql://localhost/example").database == "example"
assert COMPILER is not None and Migrator is not None and ensure_template is not None
try:
    _core.parse_dsn("postgresql://user@[broken")
except InterfaceError:
    pass
else:
    raise AssertionError("native errors must use core exception classes")

async def exercise(dsn):
    schema = f"rowx_package_{token_hex(8)}"
    async with await connect(dsn) as conn:
        assert await query[int](t"SELECT {42}::int4").one(conn) == 42
        try:
            with TemporaryDirectory() as directory:
                Path(directory, "1_initial.sql").write_text("SELECT 1;", encoding="utf-8")
                migrator = Migrator(directory, table=f"{schema}._rowx_migrations", create_schemas=(schema,))
                assert len(await migrator.run(conn)) == 1
                assert (await migrator.info(conn))[0].state == "applied"
        finally:
            await query_unchecked(t"DROP SCHEMA IF EXISTS {schema:ident} CASCADE").execute(conn)

if dsn := environ.get("ROWX_TEST_DSN"):
    asyncio.run(exercise(dsn))
assert find_spec("rowx") is None
PY

uv venv --python "${PYTHON:-3.14}" cli
uv pip install --refresh --python cli/bin/python "$distributions"/rowx-*.whl "$distributions"/rowx_core-*.whl
cli/bin/python - <<'PY'
from pathlib import Path
from sys import modules
from rowx import query
from rowx_core import query as core_query
from rowx.check.cli import main

assert query is core_query
assert "rowx_postgres" not in modules
try:
    from rowx.postgres import connect
except ImportError as error:
    assert "rowx[postgres]" in str(error)
    assert isinstance(error.__cause__, ModuleNotFoundError)
    assert error.__cause__.name == "rowx_postgres"
else:
    raise AssertionError("rowx.postgres must require the optional backend")
Path("queries.py").write_text('from rowx_core import query\nquery[int](t"SELECT 1")\n')
assert main(["check", "--database-url", "postgresql://localhost/example", "queries.py"]) == 2
PY
uv pip install --refresh --python cli/bin/python "${rowx_wheels[0]}[postgres]" "$distributions"/rowx_core-*.whl "$distributions"/rowx_postgres-*.whl
cli/bin/python - <<'PY'
import rowx_postgres
from rowx import InterfaceError, postgres, query
from rowx_core import InterfaceError as CoreInterfaceError, query as core_query

assert query is core_query
assert InterfaceError is CoreInterfaceError
assert set(postgres.__all__) == set(rowx_postgres.__all__)
for name in postgres.__all__:
    assert getattr(postgres, name) is getattr(rowx_postgres, name), name
PY
