import secrets
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from time import time

from rowx import query_unchecked
from rowx.check.scratch import ORPHAN_AGE_SECONDS, make_tag, tag_age
from rowx.errors import DatabaseError, Error
from rowx.postgres._admin import create_database, drop_database, list_databases, with_database
from rowx.postgres._connection import Connection, connect
from rowx.postgres._migrate import Migrator

type Materialize = Callable[[str, Connection, Migrator | None, str], Awaitable[None]]
type Build = Callable[[str, Connection, Migrator | None, str, Materialize], Awaitable[None]]


async def connect_lock(
    server_dsn: str,
    lock_database: str,
    lock_fallback_states: frozenset[str],
) -> tuple[Connection, str]:
    lock_dsn = with_database(server_dsn, lock_database)
    try:
        return await connect(lock_dsn), lock_dsn
    except DatabaseError as error:
        if error.sqlstate not in lock_fallback_states:
            raise
    return await connect(server_dsn), server_dsn


@asynccontextmanager
async def locked(lock_dsn: str, key: int) -> AsyncGenerator[Connection]:
    # a session lock on its own connection, released by closing it, so a crashed process never leaves it behind
    async with await connect(lock_dsn) as conn:
        await query_unchecked[tuple[()]](t"SELECT pg_catalog.pg_advisory_lock({key}::pg_catalog.int8)").execute(conn)
        yield conn


async def exists(conn: Connection, name: str) -> bool:
    (found,) = await query_unchecked[tuple[bool]](
        t"SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_database WHERE datname OPERATOR(pg_catalog.=) {name})"
    ).one(conn)
    return found


async def stale_templates(
    conn: Connection,
    project: str,
    keep: str,
    *,
    template_prefix: str,
    tmp_marker: str,
) -> list[str]:
    prefix = f"{template_prefix}{project}_"
    rows = await query_unchecked[tuple[str]](
        t"SELECT datname FROM pg_catalog.pg_database WHERE pg_catalog.starts_with(datname, {prefix}) "
        t"AND datname OPERATOR(pg_catalog.<>) {keep} "
        t"AND position({tmp_marker} IN datname) OPERATOR(pg_catalog.=) 0 ORDER BY datname"
    ).all(conn)
    return [name for (name,) in rows]


# a database left by a session that is gone (or killed): tagged over an hour ago, with no sessions. Best effort, as any
# error (a listing that is refused, a database another role owns) is ignored
async def sweep_orphans(
    server_dsn: str,
    project: str,
    *,
    test_prefix: str,
    template_prefix: str,
    tmp_marker: str,
) -> None:
    now = time()
    with suppress(Error, OSError):
        found = [
            *await list_databases(server_dsn, f"{test_prefix}{project}_"),
            *(
                info
                for info in await list_databases(server_dsn, f"{template_prefix}{project}_")
                if tmp_marker in info.name
            ),
        ]
        for info in found:
            age = tag_age(info.comment, now)
            if age is None or age <= ORPHAN_AGE_SECONDS or info.sessions > 0:
                continue
            with suppress(Error, OSError):
                await drop_database(server_dsn, info.name)


async def _build(  # ruff: ignore[too-many-arguments]
    server_dsn: str,
    lock: Connection,
    migrator: Migrator | None,
    name: str,
    *,
    tmp_marker: str,
    base_template: str,
) -> None:
    tmp = f"{name}{tmp_marker}{secrets.token_hex(8)}"
    await create_database(server_dsn, tmp, template=base_template, comment=make_tag())
    try:
        if migrator is not None:
            # closed before the rename, so nothing is connected to the template when it is cloned
            async with await connect(with_database(server_dsn, tmp)) as conn:
                await migrator.run(conn)
        # utility statements take no parameters, so the names are spliced as identifiers
        await query_unchecked[tuple[()]](t"ALTER DATABASE {tmp:ident} RENAME TO {name:ident}").execute(lock)
    except BaseException:
        with suppress(Error, OSError):
            await drop_database(server_dsn, tmp, force=True)
        raise
    # the name now exists, so a failure from here on is cleaned up under it
    try:
        await query_unchecked[tuple[()]](t"COMMENT ON DATABASE {name:ident} IS NULL").execute(lock)
        await query_unchecked[tuple[()]](
            t"ALTER DATABASE {name:ident} WITH IS_TEMPLATE true ALLOW_CONNECTIONS false"
        ).execute(lock)
    except BaseException:
        with suppress(Error, OSError):
            await drop_database(server_dsn, name, force=True)
        raise


async def drop_stale(  # ruff: ignore[too-many-arguments]
    server_dsn: str,
    lock: Connection,
    project: str,
    keep: str,
    *,
    lock_key: int,
    template_key: Callable[[str], int],
    template_prefix: str,
    tmp_marker: str,
) -> None:
    for stale in await stale_templates(lock, project, keep, template_prefix=template_prefix, tmp_marker=tmp_marker):
        key = template_key(stale)
        # a template another live session holds (its shared lock) is still in use, even while no clone is running
        (free,) = await query_unchecked[tuple[bool]](
            t"SELECT pg_catalog.pg_try_advisory_lock({lock_key}::pg_catalog.int4, {key}::pg_catalog.int4)"
        ).one(lock)
        if not free:
            continue
        try:
            # best effort and never FORCE: a template of another role, or one in use, stays
            with suppress(Error, OSError):
                await query_unchecked[tuple[()]](t"ALTER DATABASE {stale:ident} WITH IS_TEMPLATE false").execute(lock)
                await drop_database(server_dsn, stale)
        finally:
            await query_unchecked[tuple[()]](
                t"SELECT pg_catalog.pg_advisory_unlock({lock_key}::pg_catalog.int4, {key}::pg_catalog.int4)"
            ).execute(lock)


# reuses the template or builds it, under the build lock (`lock`) taken before the lookup
async def prepare(  # ruff: ignore[too-many-arguments]
    server_dsn: str,
    lock: Connection,
    migrator: Migrator | None,
    name: str,
    *,
    keep_stale: bool,
    project: str,
    lock_key: int,
    template_key: Callable[[str], int],
    template_prefix: str,
    tmp_marker: str,
    base_template: str,
    build: Build,
) -> None:
    # a database under the final name is complete by construction: it is renamed there only once migrated
    if await exists(lock, name):
        return

    async def materialize(
        server_dsn: str,
        lock: Connection,
        migrator: Migrator | None,
        name: str,
        /,
    ) -> None:
        await _build(server_dsn, lock, migrator, name, tmp_marker=tmp_marker, base_template=base_template)

    # `build` is the plugin function, so a test can wrap the one call that creates a template
    await build(server_dsn, lock, migrator, name, materialize)
    if not keep_stale:
        await drop_stale(
            server_dsn,
            lock,
            project,
            name,
            lock_key=lock_key,
            template_key=template_key,
            template_prefix=template_prefix,
            tmp_marker=tmp_marker,
        )


# returns a connection that holds the session's claim on the template until it is closed. The claim is taken under the
# build lock, so no other session's cleanup can drop the template between the lookup and the claim
async def ensure_template(  # ruff: ignore[too-many-arguments]
    server_dsn: str,
    migrator: Migrator | None,
    name: str,
    *,
    keep_stale: bool,
    lock_database: str,
    lock_fallback_states: frozenset[str],
    lock_key: int,
    template_key: Callable[[str], int],
    build_key: int,
    project: str,
    test_prefix: str,
    template_prefix: str,
    tmp_marker: str,
    base_template: str,
    build: Build,
) -> Connection:
    await sweep_orphans(
        server_dsn,
        project,
        test_prefix=test_prefix,
        template_prefix=template_prefix,
        tmp_marker=tmp_marker,
    )
    hold, lock_dsn = await connect_lock(server_dsn, lock_database, lock_fallback_states)
    key = template_key(name)
    try:
        async with locked(lock_dsn, build_key) as lock:
            await query_unchecked[tuple[()]](
                t"SELECT pg_catalog.pg_advisory_lock_shared({lock_key}::pg_catalog.int4, {key}::pg_catalog.int4)"
            ).execute(hold)
            await prepare(
                server_dsn,
                lock,
                migrator,
                name,
                keep_stale=keep_stale,
                project=project,
                lock_key=lock_key,
                template_key=template_key,
                template_prefix=template_prefix,
                tmp_marker=tmp_marker,
                base_template=base_template,
                build=build,
            )
    except BaseException:
        await hold.close()
        raise
    return hold


async def clone(server_dsn: str, template: str, project: str, *, test_prefix: str) -> str:
    name = f"{test_prefix}{project}_{secrets.token_hex(8)}"
    await create_database(server_dsn, name, template=template, comment=make_tag())
    return name
