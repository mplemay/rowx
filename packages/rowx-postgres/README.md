# rowx-postgres

PostgreSQL execution, migrations and database services for rowx.

Install `rowx[postgres]` for the checker and PostgreSQL backend, or `rowx-postgres` alone
for an application that uses `rowx_core` and `rowx_postgres` without the CLI.

Free-threaded CPython 3.14t cannot install the abi3 wheels, so pip builds rowx-postgres from the source distribution.
