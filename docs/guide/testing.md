# Testing

`rowx.testing` is a pytest plugin that gives each test a freshly migrated database. It is opt-in: install the extra and
name the plugin in a `conftest.py`.

```sh
uv add "rowx[postgres,pytest]"
```

```python
# conftest.py
pytest_plugins = ["rowx.testing"]
```

(`-p rowx.testing` on the command line works too. `import rowx` never imports pytest, and nothing loads without this.)

| Fixture | Value |
|---|---|
| `rowx_database_url` | The URL of a database cloned for this test. |
| `rowx_pool` | An open `Pool` on that database. |
| `rowx_connection` | A `Connection` to it. |

The fixtures are synchronous, so an async test uses the plugin's database through `asyncio.run`, or runs under
pytest-asyncio or anyio. The plugin supports **asyncio only**: rowx awaitables are asyncio futures, so anyio works on
its asyncio backend, and Trio does not.

```python
# test_signup.py
from rowx import query
from rowx_postgres import Pool


async def test_signup(rowx_pool: Pool) -> None:  # with pytest-asyncio or anyio's asyncio backend
    assert await query[int](t'SELECT count(*) AS "n!" FROM accounts').one(rowx_pool) == 0
```

Configuration:

- `rowx_database_url_env` (a `pytest.ini` or `[tool.pytest.ini_options]` option) names the environment variable that
  holds the server URL, so a password isn't written into the ini file. It defaults to `database_url_env` in
  `[tool.rowx]` (`DATABASE_URL`). The role needs `CREATEDB`. With no URL the tests skip, or fail when
  `rowx_require_database = true`.
- The migrations are `dir` (default `migrations/`) from `[tool.rowx.migrate]` in the `pyproject.toml` at pytest's
  rootdir or above it. `rowx_migrations` (relative to the rootdir) overrides it. Keep a `[tool.pytest.ini_options]`
  table next to the `pyproject.toml` so pytest picks that directory as its rootdir. A missing migrations directory
  fails the session. With neither `[tool.rowx.migrate]` nor `rowx_migrations`, tests get an empty database.
- `--rowx-keep-templates` keeps this project's stale templates instead of dropping them.

```toml
[tool.pytest.ini_options]
rowx_database_url_env = "TEST_DATABASE_URL"
rowx_migrations = "db/migrations"
rowx_require_database = true
```

How the template works:

- The plugin migrates **once per session**, into a template database named `rowx_tpl_<p>_<h>`. `<p>` is the first 8 hex
  characters of the SHA-256 of the migrations path, so it identifies the project. `<h>` hashes the rowx version, the
  path, the history table, `create_schemas` and the `(version, checksum)` list of the forward migrations, so editing a
  migration builds a new template, and an unchanged directory reuses the existing one across sessions and across
  processes (such as `pytest -n 2` workers) that share the server.
- A build takes an advisory lock for `<h>` before it looks for the template, so concurrent sessions build it once. It
  migrates a `_tmp_` database and renames it to the final name, so a database under that name is always complete.
  Building a new template drops this project's stale `rowx_tpl_<p>_*` databases that no session is using, and never
  another project's.
- Each test runs `CREATE DATABASE rowx_test_<p>_<hex> TEMPLATE rowx_tpl_<p>_<h>` and drops the clone with `WITH (FORCE)`
  afterwards, so no test sees another's rows. Clones and `_tmp_` builds carry a `COMMENT` with the process, host and
  time, and the next session sweeps this project's that are over an hour old and have no connections, such as the
  leftovers of a killed run.
- Roles are cluster-wide, so a test that creates one isn't isolated by the clone.

```python test
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

CONFTEST = 'pytest_plugins = ["rowx.testing"]\n'
PYPROJECT = """
[tool.rowx]
database_url_env = "DATABASE_URL"

[tool.rowx.migrate]
dir = "migrations"
"""
TEST = """
import asyncio

from rowx import query


def test_each_test_gets_a_clean_migrated_database(rowx_pool):
    async def work():
        await query(t"INSERT INTO accounts (id) VALUES (1)").execute(rowx_pool)
        return await query[int](t'SELECT count(*) AS "n!" FROM accounts').one(rowx_pool)

    assert asyncio.run(work()) == 1


def test_another_test_starts_empty(rowx_pool):
    count = asyncio.run(query[int](t'SELECT count(*) AS "n!" FROM accounts').one(rowx_pool))
    assert count == 0
"""

with TemporaryDirectory() as tmp:
    root = Path(tmp)
    (root / "migrations").mkdir()
    (root / "migrations" / "0001_accounts.sql").write_text("CREATE TABLE accounts (id int PRIMARY KEY);\n")
    (root / "conftest.py").write_text(CONFTEST)
    (root / "pyproject.toml").write_text(PYPROJECT)
    (root / "test_accounts.py").write_text(TEST)
    # the plugin wants a server URL; the DSN of this page's tests pins a search_path, so its admin URL stands in
    server_url = os.environ["ROWX_TEST_DSN"]
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q"],
        cwd=root,
        env={**os.environ, "DATABASE_URL": server_url},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
```

A complete project, with migrations, an application module, drift-checked queries and a `conftest.py`, is in
[`examples/migrations`](https://github.com/mplemay/rowx/tree/main/examples/migrations).
