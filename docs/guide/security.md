# Security

rowx trusts what you write: the static text of a t-string, migration files, `reset_query`, the DSN and `ConnectOptions`.
It treats three things as untrusted: the values you interpolate (and the values you give `:ident`, and their types), the
bytes a server sends when `sslmode` is weaker than `verify-full`, and the session state that application SQL can change
and that outlives a pool checkout (`SET ROLE`, temp tables, `SET client_encoding`). The rule it holds is that no
untrusted input can change the parse tree of a statement, or the shape of an array or range parameter. The contract is
in [Security and safety](../reference/security.md); this section is what it asks of your code.

## SQL injection

- **Values are always bound.** Every `{x}` becomes a server-side parameter, never part of the SQL text. `query` rejects
  conversions, format specs and nested templates, and there is no public raw-SQL escape. A value that looks like SQL is
  only a value.
- **`query` plus `rowx check` in CI is the gate.** Dynamic SQL needs `query_unchecked`, whose SQL the checker
  does not describe (it still resolves `T`), so keep it in small functions that take their dynamic parts from an
  allowlist ([below](#identifiers)). The checker's
  `dynamic-query` and `template-construction` rules find the rest ([The checker](checker.md)), and
  [CI recipe](ci.md) runs them on every push.
- **`Template(...)` with a non-literal string is raw SQL.** It is the one way to put attacker-controlled text into a
  statement, and the runtime compiles whatever `Template` it is given. Build dynamic SQL by nesting t-string literals
  instead. `template-construction` rejects the hand-built form in every file under the include roots. It is
  best-effort: see [Hand-built templates](checker.md#hand-built-templates) for what it can't see.
- **Compile limits.** A NUL character in the SQL text, and more than 65535 parameters after nested templates are
  flattened, are `TypeError`s raised before any IO. The connection stays usable. A list bound with `= ANY({ids})` is one
  parameter however long it is, so it never meets the second limit.

```python test
import asyncio

from rowx import query
from rowx.postgres import connect


async def main() -> None:
    async with await connect(DSN) as conn:
        # text that looks like SQL is a name that nobody has
        name = "nobody' OR true --"
        assert await query[int](t"SELECT id FROM users WHERE name = {name}").optional(conn) is None
        assert await query[int](t'SELECT count(*) AS "n!" FROM users').one(conn) == 3

        # the NUL is in the static text, so the compiler refuses the statement
        try:
            await query[bool](t"SELECT '\x00' IS NULL").one(conn)
        except TypeError as error:
            assert "NUL" in str(error)
        else:
            raise AssertionError
        assert await query[int](t"SELECT 1").one(conn) == 1


asyncio.run(main())
```

## Dynamic SQL patterns

Most "dynamic" SQL needs no `query_unchecked`. Three patterns cover the usual cases.

- **`= ANY({ids})` instead of an `IN` list.** Bind the list as one array. There is no list of placeholders to build, and
  an empty list is fine.
- **Optional filters: `({x}::T IS NULL OR col = {x})`.** One static statement serves every combination of filters,
  because a `None` turns its condition off. The cast gives the server the parameter's type, and the same expression
  twice is two parameters. `query` and the checker still apply.
- **`ORDER BY` direction through nested literal templates.** A direction can't be a parameter, so choose between two
  t-string literals (`t"ASC" if asc else t"DESC"`) and nest the result. The nested template is flattened and its values
  stay bound. This needs `query_unchecked`.

```python test
import asyncio
from decimal import Decimal

from rowx import query, query_unchecked
from rowx.postgres import Connection, connect


async def by_id(conn: Connection, ids: list[int]) -> list[str]:
    return await query[str](t"SELECT name FROM products WHERE id = ANY({ids}) ORDER BY id").all(conn)


async def search(conn: Connection, category: str | None = None, max_price: Decimal | None = None) -> list[str]:
    return await query[str](
        t"""
        SELECT name FROM products
        WHERE ({category}::text IS NULL OR category = {category})
          AND ({max_price}::numeric IS NULL OR price <= {max_price})
        ORDER BY id
        """,
    ).all(conn)


async def by_price(conn: Connection, *, ascending: bool) -> list[str]:
    direction = t"ASC" if ascending else t"DESC"
    return await query_unchecked[str](t"SELECT name FROM products ORDER BY price {direction}, id").all(conn)


async def main() -> None:
    async with await connect(DSN) as conn:
        await query(t"CREATE TABLE products (id int PRIMARY KEY GENERATED ALWAYS AS IDENTITY, name text NOT NULL, category text NOT NULL, price numeric(8, 2) NOT NULL)").execute(conn)
        await query(
            t"""
            INSERT INTO products (name, category, price) VALUES
                ('Anvil', 'hardware', 49.00), ('Birdseed', 'garden', 4.50), ('Catapult', 'hardware', 129.99)
            """,
        ).execute(conn)

        assert await by_id(conn, [3, 1]) == ["Anvil", "Catapult"]
        assert await by_id(conn, []) == []
        # still one parameter, where an IN list of this length would be over the limit
        assert await by_id(conn, list(range(70_000))) == ["Anvil", "Birdseed", "Catapult"]

        assert await search(conn) == ["Anvil", "Birdseed", "Catapult"]
        assert await search(conn, category="hardware") == ["Anvil", "Catapult"]
        assert await search(conn, max_price=Decimal(50)) == ["Anvil", "Birdseed"]
        assert await search(conn, category="hardware", max_price=Decimal(50)) == ["Anvil"]

        assert await by_price(conn, ascending=True) == ["Birdseed", "Anvil", "Catapult"]
        assert await by_price(conn, ascending=False) == ["Catapult", "Anvil", "Birdseed"]


asyncio.run(main())
```

## Identifiers

A table or column name can't be a parameter, so `{name:ident}` (in `query_unchecked`) emits a quoted identifier. rowx
makes the quoting safe for any name: it reads the name once as UTF-8 bytes, so a `str` subclass that overrides its
methods has no effect, doubles every `"`, and raises `TypeError` for an empty name, a name over 63 bytes, a control
character, or an `:ident` inside a string literal or comment. Tuples join with dots.

What rowx can't decide is which names are allowed. Quoting makes a name safe as an identifier, but not harmless as a
choice, and identifiers are deliberately **not** restricted to enum members. If a user can pick the name, the
application must allowlist it.

The risk is the **sort-order oracle**. A caller who can choose the `ORDER BY` column can sort by a column they can't
select, such as a cost or a salary, and read the order of the rows they can see. That order says how the hidden values
compare, and repeated requests with different filters narrow it down. No value is ever returned, and nothing in the SQL
is malformed, so only the allowlist stops it.

Use a `StrEnum` for names that arrive as strings, and a `Literal` with `assert_never` where you already hold a typed
value. Parse the raw input at the edge of the application, so a value that isn't listed fails before any SQL is built.

```python test
import asyncio
from enum import StrEnum
from string.templatelib import Template
from typing import Literal, assert_never

from rowx import query, query_unchecked
from rowx.postgres import Connection, connect


class SortColumn(StrEnum):
    NAME = "name"
    PRICE = "price"


type Direction = Literal["asc", "desc"]
type GroupColumn = Literal["name", "category"]


# a StrEnum is an allowlist that also validates raw input: an unknown value is a ValueError
def parse_sort(raw: str) -> SortColumn:
    return SortColumn(raw)


# a Literal is checked by the type checker only, so a match is the runtime check, and assert_never makes the type
# checker report a case that was added to the Literal but not handled here
def direction_sql(direction: Direction) -> Template:
    match direction:
        case "asc":
            return t"ASC"
        case "desc":
            return t"DESC"
        case _:
            assert_never(direction)


def group_sql(column: GroupColumn) -> Template:
    match column:
        case "name" | "category":
            return t"{column:ident}"
        case _:
            assert_never(column)


async def listing(conn: Connection, sort: SortColumn, direction: Direction) -> list[str]:
    return await query_unchecked[str](
        t"SELECT name FROM products ORDER BY {sort:ident} {direction_sql(direction)}, id",
    ).all(conn)


async def counts(conn: Connection, column: GroupColumn) -> list[tuple[str, int]]:
    grouped = group_sql(column)
    return await query_unchecked[tuple[str, int]](
        t'SELECT {grouped}, count(*)::int4 AS "n!" FROM products GROUP BY 1 ORDER BY 1',
    ).all(conn)


async def main() -> None:
    async with await connect(DSN) as conn:
        await query(t"CREATE TABLE products (id int PRIMARY KEY GENERATED ALWAYS AS IDENTITY, name text NOT NULL, category text NOT NULL, price numeric(8, 2) NOT NULL, internal_cost numeric(8, 2) NOT NULL)").execute(conn)
        await query(
            t"""
            INSERT INTO products (name, category, price, internal_cost) VALUES
                ('Anvil', 'hardware', 49.00, 30.00), ('Birdseed', 'garden', 4.50, 4.00),
                ('Catapult', 'hardware', 129.99, 20.00), ('Dynamite', 'hardware', 19.95, 10.00)
            """,
        ).execute(conn)

        assert await listing(conn, parse_sort("price"), "desc") == ["Catapult", "Anvil", "Dynamite", "Birdseed"]
        assert await listing(conn, SortColumn.NAME, "asc") == ["Anvil", "Birdseed", "Catapult", "Dynamite"]
        assert await counts(conn, "category") == [("garden", 1), ("hardware", 3)]

        # the application rejects names that aren't listed, before rowx sees them
        for raw in ("internal_cost", "price; SELECT 1", "PRICE"):
            try:
                parse_sort(raw)
            except ValueError:
                pass
            else:
                raise AssertionError

        # why: rowx would quote the column and run the statement, and the order of the rows would then say that
        # Birdseed costs the least to make and Anvil the most, although no cost was selected
        hidden = "internal_cost"
        leaked = await query_unchecked[str](t"SELECT name FROM products ORDER BY {hidden:ident}").all(conn)
        assert leaked == ["Birdseed", "Dynamite", "Catapult", "Anvil"]
        assert leaked != await listing(conn, SortColumn.PRICE, "asc")


asyncio.run(main())
```

The full pattern, a filtered and sortable listing that is also checked in CI, is in
[`examples/security`](https://github.com/mplemay/rowx/tree/main/examples/security).

## Pool reset

When a connection goes back to a pool, the next checkout inherits whatever the last one left. For a pool that serves
more than one tenant or role, that is a leak. The default `RESET_QUERY` leaves a connection with none of the previous
checkout's session state:

- the session user and the role (`RESET SESSION AUTHORIZATION`; `RESET ALL` does not cover them);
- every setting (`RESET ALL`), custom settings such as `app.tenant` included, and the `search_path`;
- advisory locks, open cursors and `LISTEN`s;
- temporary tables (`DISCARD TEMP`). A leftover temp table would shadow an application table of the same name, because
  `pg_temp` is searched first.

It does not remove statements made with SQL-level `PREPARE`, which is unsupported across checkouts. The reset is
preceded by a `ROLLBACK` when a transaction may be open, and by an idle check when one may not be, so a reset that finds
a transaction open fails and the pool discards the connection instead of reusing it.
[Hooks and the reset](executors.md#hooks-and-the-reset) has the details and the order.

**`reset_query=None` is unsafe for a pool that serves more than one tenant or role.** Nothing then clears the role, the
`search_path`, temp tables, advisory locks or settings. A custom `reset_query` replaces the default entirely, so keep
every part you rely on.

```python test
import asyncio

from rowx import query
from rowx.postgres import Connection, Pool

type Leftovers = tuple[str | None, bool, int, int]


async def dirty(pool: Pool) -> None:
    async with pool.acquire() as conn:
        await query(t"SET app.tenant = 'acme'").execute(conn)
        await query(t"CREATE TEMP TABLE scratch (n int)").execute(conn)
        assert await query[bool](t"SELECT pg_try_advisory_lock(7)").one(conn)


async def leftovers(pool: Pool) -> Leftovers:
    async with pool.acquire() as conn:
        return await state(conn)


async def state(conn: Connection) -> Leftovers:
    return (
        await query[str | None](t"SELECT current_setting('app.tenant', true)").one(conn),
        await query[bool](t"SELECT to_regclass('pg_temp.scratch') IS NOT NULL").one(conn),
        await query[int](
            t"SELECT count(*)::int4 FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()",
        ).one(conn),
        await query[int](t"SELECT pg_backend_pid()").one(conn),
    )


async def main() -> None:
    # one connection, so the second checkout is the first one's connection again
    async with Pool(DSN, max_size=1) as pool:
        await dirty(pool)
        tenant, scratch, locks, _ = await leftovers(pool)
        assert (tenant or None, scratch, locks) == (None, False, 0)

    async with Pool(DSN, max_size=1, reset_query=None) as pool:
        async with pool.acquire() as conn:
            first_pid = (await state(conn))[3]
        await dirty(pool)
        # the settings, the table and the lock of the previous checkout are all still there
        assert await leftovers(pool) == ("acme", True, 1, first_pid)


asyncio.run(main())
```

## TLS

TLS defaults to `prefer`, as in libpq: it uses TLS when the server offers it, without verifying the certificate, and
falls back to plaintext when the server doesn't support TLS. That is encrypted, but not authenticated. A man in the
middle can pose as the server, and ask for a cleartext or MD5 password. It also controls the bytes rowx decodes. The
decoders return `DecodeError` for malformed input and never panic, but only a verified server is one you chose.

Use `sslmode=verify-full` in production, and pair it with `channel_binding=require`:

```text
postgresql://app@db.example/app?sslmode=verify-full&sslrootcert=/etc/ssl/db-ca.pem&channel_binding=require
```

- **`sslmode=verify-full`** verifies the certificate chain and the host name, so the server is the one you meant to
  reach. It protects against an impersonating server. `verify-full` and `require` never fall back to plaintext.
- **`channel_binding=require`** makes the client refuse to answer a password or SASL request unless the server
  authenticates with `SCRAM-SHA-256-PLUS`, which ties the login to the TLS session so a man in the middle can't relay
  it. A server that asks for a cleartext or MD5 password, or for SCRAM without `-PLUS`, gets nothing before the
  connection fails. It protects against a downgrade of the password exchange. The default, `prefer`, binds when it can.

`require` together with `sslmode=disable` is an `InterfaceError` when the options are parsed. `channel_binding` is a
typed option, so it is not accepted as an `extra` key. As in libpq, a key given twice in a DSN resolves last-wins,
`channel_binding` included. The checker's own connection takes the same options, so it is as exposed as the
application's.

Channel binding uses `tls-server-end-point`, which the driver derives from the server certificate's signature algorithm.
That works for RSA (SHA-1, SHA-256, SHA-384, SHA-512) and ECDSA (SHA-256, SHA-384) certificates only. Against an Ed25519
or ECDSA-with-SHA-512 server certificate `channel_binding=require` fails to connect, and so does a server that doesn't
offer `SCRAM-SHA-256-PLUS` (`trust`, `md5`, `password` or plain `scram-sha-256` authentication). Use `prefer` there.

```python test
from rowx import InterfaceError
from rowx.postgres import ConnectOptions

# the DSN above, parsed into typed fields; nothing connects
options = ConnectOptions.from_dsn(
    "postgresql://app@db.example/app?sslmode=verify-full&sslrootcert=/etc/ssl/db-ca.pem&channel_binding=require"
)
assert options.sslmode == "verify-full"
assert options.sslrootcert == "/etc/ssl/db-ca.pem"
assert options.channel_binding == "require"
assert "channel_binding" not in options.extra

# with neither key given, the sslmode is the default (prefer) and channel binding is left to the client
default = ConnectOptions.from_dsn("postgresql://app@db.example/app")
assert (default.sslmode, default.channel_binding) == (None, None)

# binding to a TLS session that was switched off can't work, so it fails when the options are parsed
try:
    ConnectOptions.from_dsn("postgresql://app@db.example/app?sslmode=disable&channel_binding=require")
except InterfaceError as error:
    assert "channel_binding" in str(error)
else:
    raise AssertionError
```

## Session state

rowx sends UTF-8 and standard-conforming strings, and the server's parameter reports are watched.
**`SET client_encoding` to anything but UTF-8 is unsupported.** Application SQL that does it, or turns
`standard_conforming_strings` off, makes the connection fail every later call with `InterfaceError`, and a pool discards
it. The statement that did it fails too. This is a backstop against text stored in the wrong encoding, not the
protection for arrays and ranges, which are always sent in binary format. See "Session encoding drift" under
[Deployment caveats](executors.md#deployment-caveats) for the exceptions and the window.

```python test
import asyncio

from rowx import query
from rowx import InterfaceError
from rowx.postgres import Pool


async def pid(pool: Pool) -> int:
    async with pool.acquire() as conn:
        return await query[int](t"SELECT pg_backend_pid()").one(conn)


async def main() -> None:
    async with Pool(DSN, max_size=1) as pool:
        first = await pid(pool)

        async with pool.acquire() as conn:
            try:
                await query(t"SET client_encoding = 'SJIS'").execute(conn)
            except InterfaceError as error:
                assert "client_encoding" in str(error)
            else:
                raise AssertionError

            # the connection is unusable for good: setting UTF8 again doesn't clear it
            try:
                await query[int](t"SELECT 1").one(conn)
            except InterfaceError as error:
                assert "client_encoding" in str(error)
            else:
                raise AssertionError
            try:
                await query(t"SET client_encoding = 'UTF8'").execute(conn)
            except InterfaceError as error:
                assert "client_encoding" in str(error)
            else:
                raise AssertionError

        # the pool threw it away instead of reusing it
        assert await pid(pool) != first


asyncio.run(main())
```

## Connect options and secrets

Options are trusted configuration, but they reach the server as startup text, so rowx validates them. A
`server_settings` key must be a valid setting name, an `extra` key must match `^[a-z_]+$`, and a NUL or other control
character in any value is an `InterfaceError` that names the key and not the value. Without that, a newline in a key
could add a startup option. A password is never echoed: it is redacted from every exception message, `repr` and checker
diagnostic, whichever form of DSN carried it, and `ConnectOptions.__repr__` hides the values of `extra`.

```python test
import asyncio

from rowx import InterfaceError
from rowx.postgres import ConnectOptions, connect


async def main() -> None:
    # nothing listens on port 1, and the password is in no message
    refused = ConnectOptions(host="127.0.0.1", port=1, user="app", password="s3cret-pw", connect_timeout=2)
    try:
        await connect(refused)
    except InterfaceError as error:
        assert "s3cret-pw" not in str(error)
        assert "s3cret-pw" not in repr(error)
    else:
        raise AssertionError

    assert "s3cret-pw" not in repr(refused)
    parsed = ConnectOptions.from_dsn("postgresql://app:s3cret-pw@db.example/app?sslpassword=key-pw")
    assert "s3cret-pw" not in repr(parsed)
    assert "key-pw" not in repr(parsed)

    # a key with whitespace would be split into extra startup options
    try:
        await connect(ConnectOptions(host="127.0.0.1", port=1, server_settings={"work_mem\n-c role": "x"}))
    except InterfaceError as error:
        assert "malformed server setting name" in str(error)
    else:
        raise AssertionError

    # the error names the key, never the value
    try:
        await connect(ConnectOptions(host="127.0.0.1", port=1, application_name="a\x00b"))
    except InterfaceError as error:
        assert "application_name" in str(error)
        assert "a\x00b" not in str(error)
    else:
        raise AssertionError


asyncio.run(main())
```
