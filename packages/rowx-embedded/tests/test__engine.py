import asyncio
import contextlib
import fcntl
import gc
import json
import os
import shutil
import signal
import socket
import struct
import subprocess  # ruff: ignore[suspicious-subprocess-import]
import sys
import tempfile
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

# the companion is optional: without it (the root `just test-py` never installs it) this module skips cleanly
rowx_embedded = pytest.importorskip("rowx_embedded")
psycopg = pytest.importorskip("psycopg")
rowx_postgres = pytest.importorskip("rowx_postgres")
rowx_core = pytest.importorskip("rowx_core")
rowx_core_errors = pytest.importorskip("rowx_core.errors")
query = rowx_core.query

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator

    from rowx_embedded import Instance

SOCKET_NAME: Final[str] = ".s.PGSQL.5432"
ROUNDS: Final[int] = 200
MIN_SNAPSHOT_BYTES: Final[int] = 1000
SSL_REQUEST: Final[int] = 80_877_103
CANCEL_REQUEST: Final[int] = 80_877_102
STARTUP_WAIT: Final[float] = 60.0
WAITED_FOR_A_QUERY: Final[float] = 0.5
ABANDON_LIMIT: Final[float] = 10.0


def expect(actual: object, expected: object) -> None:
    if actual != expected:
        pytest.fail(f"expected {expected!r}, got {actual!r}")


def expect_true(condition: object) -> None:
    if not condition:
        pytest.fail("the condition is false")


def expect_false(condition: object) -> None:
    if condition:
        pytest.fail("the condition is true")


def run[T](body: Callable[[], Awaitable[T]]) -> T:
    return asyncio.run(body())


def socket_dir(dsn: str) -> Path:
    host = dsn.split("host=", maxsplit=1)[1].split("&", maxsplit=1)[0]
    return Path(host)


def leftovers(root: Path) -> list[str]:
    return sorted(entry.name for entry in root.iterdir() if entry.name.startswith("rxe-"))


def wait_until(condition: Callable[[], bool], *, limit: float = 15.0) -> bool:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


def raw_one(dsn: str, sql: str) -> object:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(sql).fetchone()
        return None if row is None else row[0]


@pytest.fixture
def private_tmp(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    # a short path of its own (a socket path is limited to about 100 bytes), so leftovers can be counted exactly
    root = Path(tempfile.mkdtemp(prefix="rxt-"))
    monkeypatch.setenv("TMPDIR", str(root))
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def db() -> Iterator[Instance]:
    instance = rowx_embedded.Instance.start()
    yield instance
    instance.close()


def child_env(root: Path) -> dict[str, str]:
    return {**os.environ, "TMPDIR": str(root)}


def spawn(code: str, root: Path) -> subprocess.Popen[str]:
    return subprocess.Popen(  # ruff: ignore[subprocess-without-shell-equals-true]
        [sys.executable, "-c", code],
        env=child_env(root),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )


def python(code: str, root: Path, *, timeout: float = STARTUP_WAIT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [sys.executable, "-c", code],
        env=child_env(root),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


# --- module surface -------------------------------------------------------------------------------------------


def test_the_module_exports_its_versions_and_the_cache_key_inputs() -> None:
    expect(rowx_embedded.API_VERSION, 1)
    expect(rowx_embedded.ENGINE_VERSION, "18.4")
    expect(rowx_embedded.SNAPSHOT_FORMAT, 1)
    expect(len(rowx_embedded.EXTENSIONS), 32)
    expect(list(rowx_embedded.EXTENSIONS), sorted(rowx_embedded.EXTENSIONS))
    expect_true(rowx_embedded.RUNTIME_VERSION)
    expect(len(rowx_embedded.WASM_SHA256), 64)


# --- start and close in memory --------------------------------------------------------------------------------


def test_a_memory_instance_starts_serves_and_removes_its_socket_directory(private_tmp: Path) -> None:
    instance = rowx_embedded.Instance.start()
    directory = socket_dir(instance.dsn)
    expect(directory.parent.resolve(), private_tmp.resolve())
    expect(instance.dsn, f"postgresql://postgres@/postgres?host={directory}&sslmode=disable")
    expect_true((directory / SOCKET_NAME).exists())
    expect(directory.stat().st_mode & 0o777, 0o700)
    expect(instance.boot, "fresh")
    expect(instance.directory, None)
    expect_true(rowx_embedded.ENGINE_VERSION in raw_one(instance.dsn, "SHOW server_version"))

    async def body() -> int:
        async with await rowx_postgres.connect(instance.dsn) as conn:
            return await query[int](t"SELECT 41 + 1").one(conn)

    expect(run(body), 42)
    expect(raw_one(instance.dsn, "SHOW fsync"), "off")

    instance.close()
    expect_true(instance.closed)
    expect_false(directory.exists())
    expect(leftovers(private_tmp), [])
    instance.close()  # idempotent


def test_a_closed_instance_refuses_a_snapshot() -> None:
    instance = rowx_embedded.Instance.start()
    instance.close()
    with pytest.raises(rowx_embedded.EngineError, match="closed"):
        instance.snapshot()


def test_the_context_manager_closes(private_tmp: Path) -> None:
    with rowx_embedded.Instance.start() as instance:
        expect(len(leftovers(private_tmp)), 1)
    expect_true(instance.closed)
    expect(leftovers(private_tmp), [])


# --- a persistent directory -----------------------------------------------------------------------------------


def test_a_directory_instance_survives_a_restart(tmp_path: Path) -> None:
    directory = str(tmp_path / "pg")
    first = rowx_embedded.Instance.start(directory)
    expect(first.directory, directory)
    # persistent data keeps the engine's durable defaults
    expect(raw_one(first.dsn, "SHOW fsync"), "on")
    with psycopg.connect(first.dsn, autocommit=True) as conn:
        conn.execute("CREATE TABLE kept (id int PRIMARY KEY, name text)")
        conn.execute("INSERT INTO kept VALUES (1, 'raw')")
    first.close()

    second = rowx_embedded.Instance.start(directory)

    async def body() -> list[str]:
        async with await rowx_postgres.connect(second.dsn) as conn:
            await query(t"INSERT INTO kept VALUES (2, 'rowx')").execute(conn)
            return await query[str](t"SELECT name FROM kept ORDER BY id").all(conn)

    expect(run(body), ["raw", "rowx"])
    second.close()
    expect_false((tmp_path / ".pg.rowx-owner").exists())


def test_an_identity_column_continues_without_a_gap_after_each_close_and_reopen(tmp_path: Path) -> None:
    directory = str(tmp_path / "pg")
    seen: list[int] = []
    for round_ in range(4):
        instance = rowx_embedded.Instance.start(directory)
        with psycopg.connect(instance.dsn, autocommit=True) as conn:
            if round_ == 0:
                conn.execute("CREATE TABLE ids (id int GENERATED ALWAYS AS IDENTITY, n int)")
            for _ in range(2):
                row = conn.execute("INSERT INTO ids (n) VALUES (0) RETURNING id").fetchone()
                seen.append(-1 if row is None else row[0])
        instance.close()
    expect(seen, [1, 2, 3, 4, 5, 6, 7, 8])


def test_a_directory_closed_before_any_client_connected_opens_again(tmp_path: Path) -> None:
    directory = str(tmp_path / "pg")
    rowx_embedded.Instance.start(directory).close()
    reopened = rowx_embedded.Instance.start(directory)
    expect(raw_one(reopened.dsn, "SELECT 1"), 1)
    reopened.close()


def test_a_relative_directory_is_refused() -> None:
    with pytest.raises(rowx_embedded.EngineError, match="absolute"):
        rowx_embedded.Instance.start("relative/db")


# --- the directory lock ---------------------------------------------------------------------------------------


def test_the_lock_error_names_the_pid_in_this_process(tmp_path: Path) -> None:
    directory = str(tmp_path / "pg")
    holder = rowx_embedded.Instance.start(directory)
    try:
        with pytest.raises(rowx_embedded.DirectoryInUseError) as caught:
            rowx_embedded.Instance.start(directory)
        expect(caught.value.pid, os.getpid())
        expect_true(f"is in use by pid {os.getpid()}" in str(caught.value))
        expect(caught.value.directory, directory)
        expect_true(isinstance(caught.value, rowx_embedded.EngineError))
    finally:
        holder.close()
    # a failed second start never hurts the owner, and the directory is free again after the owner closes
    rowx_embedded.Instance.start(directory).close()


def test_the_lock_error_names_the_pid_from_another_process(tmp_path: Path, private_tmp: Path) -> None:
    directory = str(tmp_path / "pg")
    holder = rowx_embedded.Instance.start(directory)
    try:
        code = (
            "import rowx_embedded as e\n"
            "try:\n"
            f"    e.Instance.start({directory!r})\n"
            "except e.DirectoryInUseError as error:\n"
            "    print(error.pid)\n"
        )
        result = python(code, private_tmp)
        expect(result.stdout.strip(), str(os.getpid()))
    finally:
        holder.close()


def test_the_lock_error_degrades_without_an_owner_file(tmp_path: Path) -> None:
    directory = tmp_path / "pg"
    holder = rowx_embedded.Instance.start(str(directory))
    try:
        (directory.parent / ".pg.rowx-owner").unlink()
        with pytest.raises(rowx_embedded.DirectoryInUseError, match="in use by another instance") as caught:
            rowx_embedded.Instance.start(str(directory))
        expect(caught.value.pid, None)
    finally:
        holder.close()


def test_a_killed_owner_leaves_a_directory_that_opens_again(tmp_path: Path, private_tmp: Path) -> None:
    directory = tmp_path / "pg"
    code = (
        "import os, signal, sys\n"
        "import rowx_embedded as e\n"
        f"db = e.Instance.start({str(directory)!r})\n"
        "print(db.dsn, flush=True)\n"
        "sys.stdin.readline()\n"
        "os.kill(os.getpid(), signal.SIGKILL)\n"
    )
    child = spawn(code, private_tmp)
    if child.stdout is None or child.stdin is None:
        pytest.fail("the child has no pipes")
    child.stdout.readline()
    expect_true(wait_until((directory.parent / ".pg.rowx-owner").exists))
    child.stdin.write("\n")
    child.stdin.flush()
    child.wait(timeout=STARTUP_WAIT)
    child.stdin.close()
    child.stdout.close()
    # the stale owner file names a dead pid, and the engine lock died with the process
    expect(int((directory.parent / ".pg.rowx-owner").read_text().strip()), child.pid)
    reopened = rowx_embedded.Instance.start(str(directory))
    reopened.close()


# --- a second client, and reconnecting ------------------------------------------------------------------------


def test_a_concurrent_second_client_gets_53300_with_the_hint(db: Instance) -> None:
    with psycopg.connect(db.dsn, autocommit=True) as first:
        expect(first.execute("SELECT 1").fetchone(), (1,))
        with pytest.raises(psycopg.OperationalError) as caught:
            psycopg.connect(db.dsn)
        expect_true("too many clients already" in str(caught.value))
        expect_true("an embedded database serves one session at a time; use max_size=1" in str(caught.value))
        # the refused client never disturbs the live session
        expect(first.execute("SELECT 2").fetchone(), (2,))
    expect(raw_one(db.dsn, "SELECT 3"), 3)


def test_a_concurrent_second_rowx_client_gets_53300(db: Instance) -> None:
    async def body() -> None:
        async with await rowx_postgres.connect(db.dsn) as conn:
            with pytest.raises(rowx_core_errors.DatabaseError) as caught:
                await rowx_postgres.connect(db.dsn)
            expect_true("53300" in str(caught.value))
            expect(await query[int](t"SELECT 1").one(conn), 1)

    run(body)


def test_200_close_then_connect_rounds_never_see_53300(db: Instance) -> None:
    for round_ in range(ROUNDS):
        with psycopg.connect(db.dsn, autocommit=True) as conn:
            expect(conn.execute("SELECT %s", (round_,)).fetchone(), (round_,))


def test_200_close_then_connect_rounds_never_see_53300_through_rowx(db: Instance) -> None:
    async def body() -> int:
        total = 0
        for round_ in range(ROUNDS):
            async with await rowx_postgres.connect(db.dsn) as conn:
                total += await query[int](t"SELECT {round_}::int").one(conn)
        return total

    expect(run(body), sum(range(ROUNDS)))


# --- cancel ---------------------------------------------------------------------------------------------------


def test_a_cancelled_pg_sleep_leaves_the_rowx_connection_usable(db: Instance) -> None:
    async def body() -> None:
        async with await rowx_postgres.connect(db.dsn) as conn:
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.3):
                    await query(t"SELECT pg_sleep(2)").execute(conn)
            # the engine cannot interrupt the statement; the next query waits for it, then runs
            expect(await query[int](t"SELECT 1").one(conn), 1)
            expect(await query[int](t"SELECT 2").one(conn), 2)

    run(body)


def test_a_cancelled_pg_sleep_leaves_the_raw_connection_usable(db: Instance) -> None:
    with psycopg.connect(db.dsn, autocommit=True) as conn:
        timer = threading.Timer(0.3, conn.cancel)
        timer.start()
        try:
            conn.execute("SELECT pg_sleep(2)")
        finally:
            timer.join()
        expect(conn.execute("SELECT 1").fetchone(), (1,))


def test_the_proxy_answers_ssl_with_n_and_closes_a_cancel_request_at_once(db: Instance) -> None:
    path = str(socket_dir(db.dsn) / SOCKET_NAME)
    with socket.socket(socket.AF_UNIX) as ssl_probe:
        ssl_probe.connect(path)
        ssl_probe.sendall(struct.pack("!ii", 8, SSL_REQUEST))
        expect(ssl_probe.recv(8), b"N")
    with socket.socket(socket.AF_UNIX) as cancel_probe:
        cancel_probe.settimeout(5)
        cancel_probe.connect(path)
        cancel_probe.sendall(struct.pack("!iiii", 16, CANCEL_REQUEST, 1, 2))
        expect(cancel_probe.recv(8), b"")
    with socket.socket(socket.AF_UNIX) as garbage:
        garbage.settimeout(5)
        garbage.connect(path)
        garbage.sendall(b"GET / HTTP/1.1\r\n\r\n")
        expect(garbage.recv(8), b"")
    expect(raw_one(db.dsn, "SELECT 1"), 1)


def test_a_database_other_than_postgres_is_refused(db: Instance) -> None:
    with pytest.raises(psycopg.OperationalError, match="does not exist"):
        psycopg.connect(db.dsn.replace("/postgres?", "/other?"))
    expect(raw_one(db.dsn, "SELECT 1"), 1)


# --- session state is gone for the next session ---------------------------------------------------------------


def test_a_set_and_a_prepared_statement_are_gone_for_the_next_raw_session(db: Instance) -> None:
    default_work_mem = raw_one(db.dsn, "SHOW work_mem")
    with psycopg.connect(db.dsn, autocommit=True) as conn:
        conn.execute("SET work_mem = '7MB'")
        conn.execute("PREPARE keep_me AS SELECT 99")
        conn.execute("CREATE TEMP TABLE scratch (i int)")
        expect(conn.execute("SHOW work_mem").fetchone(), ("7MB",))
    with psycopg.connect(db.dsn, autocommit=True) as conn:
        expect(conn.execute("SHOW work_mem").fetchone(), (default_work_mem,))
        with pytest.raises(psycopg.errors.InvalidSqlStatementName):
            conn.execute("EXECUTE keep_me")
        with pytest.raises(psycopg.errors.UndefinedTable):
            conn.execute("SELECT * FROM scratch")


def test_a_set_and_a_prepared_statement_are_gone_for_the_next_rowx_session(db: Instance) -> None:
    async def body() -> None:
        async with await rowx_postgres.connect(db.dsn) as conn:
            default = await query[str](t"SHOW work_mem").one(conn)
            await query(t"SET work_mem = '7MB'").execute(conn)
            await query(t"PREPARE keep_me AS SELECT 99").execute(conn)
            expect(await query[str](t"SHOW work_mem").one(conn), "7MB")
        async with await rowx_postgres.connect(db.dsn) as conn:
            expect(await query[str](t"SHOW work_mem").one(conn), default)
            with pytest.raises(rowx_core_errors.DatabaseError):
                await query(t"EXECUTE keep_me").execute(conn)

    run(body)


def test_a_transaction_left_open_is_rolled_back_for_the_next_session(db: Instance) -> None:
    conn = psycopg.connect(db.dsn)
    conn.execute("CREATE TABLE half (i int)")
    conn.commit()
    conn.execute("INSERT INTO half VALUES (1)")
    # closing without a commit abandons the open transaction
    conn.close()
    expect(raw_one(db.dsn, "SELECT count(*) FROM half"), 0)


def test_startup_options_survive_a_client_reset_all(db: Instance) -> None:
    dsn = f"{db.dsn}&options=-c%20work_mem%3D5MB&application_name=probe"
    with psycopg.connect(dsn, autocommit=True) as conn:
        expect(conn.execute("SHOW work_mem").fetchone(), ("5MB",))
        conn.execute("SET work_mem = '9MB'")
        conn.execute("RESET ALL")
        expect(conn.execute("SHOW work_mem").fetchone(), ("5MB",))
        conn.execute("DISCARD ALL")
        expect(conn.execute("SHOW work_mem").fetchone(), ("5MB",))
        expect(conn.execute("SHOW application_name").fetchone(), ("probe",))
    expect_true(raw_one(db.dsn, "SHOW work_mem") != "5MB")


# --- snapshots ------------------------------------------------------------------------------------------------


def seed_data(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION citext")
        conn.execute("CREATE TABLE people (id int PRIMARY KEY, name citext NOT NULL)")
        conn.execute("INSERT INTO people SELECT i, 'person ' || i FROM generate_series(1, 50) AS i")


def test_a_snapshot_boots_into_a_new_instance_with_the_data(private_tmp: Path) -> None:
    source = rowx_embedded.Instance.start()
    seed_data(source.dsn)
    snapshot = source.snapshot()
    expect_true(isinstance(snapshot, bytes))
    # the source keeps running, and a second snapshot works
    expect(raw_one(source.dsn, "SELECT count(*) FROM people"), 50)
    expect_true(len(source.snapshot()) > MIN_SNAPSHOT_BYTES)
    source.close()

    first = rowx_embedded.Instance.start(snapshot=snapshot)
    expect(first.boot, "seed")
    expect(raw_one(first.dsn, "SELECT count(*) FROM people"), 50)
    expect_true(raw_one(first.dsn, "SELECT name = 'PERSON 7' FROM people WHERE id = 7"))
    expect(raw_one(first.dsn, "SHOW fsync"), "off")

    async def body() -> None:
        async with await rowx_postgres.connect(first.dsn) as conn:
            await query(t"INSERT INTO people VALUES (1000, 'only here')").execute(conn)
            expect(await query[int](t"SELECT count(*) FROM people").one(conn), 51)

    run(body)
    # a second instance from the same snapshot is independent, and boots from the cached seed
    second = rowx_embedded.Instance.start(snapshot=snapshot)
    expect(raw_one(second.dsn, "SELECT count(*) FROM people"), 50)
    first.close()
    second.close()
    expect(leftovers(private_tmp), [])


def test_a_snapshot_restores_into_a_directory(tmp_path: Path) -> None:
    source = rowx_embedded.Instance.start()
    seed_data(source.dsn)
    snapshot = source.snapshot()
    source.close()
    directory = str(tmp_path / "restored")
    restored = rowx_embedded.Instance.start(directory, snapshot)
    expect(restored.boot, "restore")
    expect(raw_one(restored.dsn, "SELECT count(*) FROM people"), 50)
    restored.close()
    reopened = rowx_embedded.Instance.start(directory)
    expect(raw_one(reopened.dsn, "SELECT count(*) FROM people"), 50)
    reopened.close()


def test_a_snapshot_taken_during_a_client_transaction_holds_only_committed_data(db: Instance) -> None:
    seed_data(db.dsn)
    with psycopg.connect(db.dsn) as conn:
        conn.execute("INSERT INTO people VALUES (1000, 'uncommitted')")
        snapshot = db.snapshot()
        conn.rollback()
    copy = rowx_embedded.Instance.start(snapshot=snapshot)
    try:
        expect(raw_one(copy.dsn, "SELECT count(*) FROM people"), 50)
    finally:
        copy.close()


def rewrite_header(snapshot: bytes, **changes: object) -> bytes:
    magic, rest = snapshot[:8], snapshot[8:]
    (length,) = struct.unpack(">I", rest[:4])
    header = json.loads(rest[4 : 4 + length])
    header.update(changes)
    encoded = json.dumps(header).encode()
    return magic + struct.pack(">I", len(encoded)) + encoded + rest[4 + length :]


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"engine_version": "18.5"}, "engine version"),
        ({"runtime_version": "9.9.9"}, "runtime version"),
        ({"wasm_sha256": "0" * 64}, "wasm hash"),
        ({"extensions": ["citext"]}, "extension set"),
        ({"format": 99}, "snapshot format"),
    ],
)
def test_a_mismatched_snapshot_is_a_clear_error(changes: dict[str, object], expected: str) -> None:
    source = rowx_embedded.Instance.start()
    snapshot = source.snapshot()
    source.close()
    with pytest.raises(rowx_embedded.SnapshotError) as caught:
        rowx_embedded.Instance.start(snapshot=rewrite_header(snapshot, **changes))
    expect_true(expected in str(caught.value))
    expect_true("take a new snapshot" in str(caught.value))


def test_garbage_and_truncated_snapshots_are_refused(private_tmp: Path) -> None:
    source = rowx_embedded.Instance.start()
    snapshot = source.snapshot()
    source.close()
    with pytest.raises(rowx_embedded.SnapshotError, match="not a rowx-embedded snapshot"):
        rowx_embedded.Instance.start(snapshot=b"PK\x03\x04 definitely not ours")
    with pytest.raises(rowx_embedded.SnapshotError, match="truncated"):
        rowx_embedded.Instance.start(snapshot=snapshot[:-1])
    # a flipped payload byte is caught by the recorded digest before anything boots
    corrupted = bytearray(snapshot)
    corrupted[-100] ^= 0xFF
    with pytest.raises(rowx_embedded.SnapshotError, match="digest"):
        rowx_embedded.Instance.start(snapshot=bytes(corrupted))
    expect(leftovers(private_tmp), [])


def test_four_concurrent_instances_keep_their_own_data(private_tmp: Path) -> None:
    sums: dict[int, int] = {}
    errors: list[BaseException] = []

    def exercise(index: int) -> int:
        instance = rowx_embedded.Instance.start()
        try:
            with psycopg.connect(instance.dsn, autocommit=True) as conn:
                conn.execute("CREATE TABLE mine (v int)")
                conn.execute("INSERT INTO mine SELECT %s * 1000 + i FROM generate_series(1, 10) AS i", (index,))
                row = conn.execute("SELECT sum(v) FROM mine").fetchone()
                return 0 if row is None else int(row[0])
        finally:
            instance.close()

    def work(index: int) -> None:
        try:
            sums[index] = exercise(index)
        except BaseException as error:  # ruff: ignore[blind-except]
            errors.append(error)

    threads = [threading.Thread(target=work, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=STARTUP_WAIT)
    expect(errors, [])
    expect(sums, {index: index * 10_000 + 55 for index in range(4)})
    expect(leftovers(private_tmp), [])


def test_four_concurrent_instances_through_rowx(private_tmp: Path) -> None:
    async def one(index: int) -> int:
        instance = await asyncio.to_thread(rowx_embedded.Instance.start)
        try:
            async with await rowx_postgres.connect(instance.dsn) as conn:
                await query(t"CREATE TABLE mine (v int)").execute(conn)
                await query(t"INSERT INTO mine SELECT {index} * 1000 + i FROM generate_series(1, 10) AS i").execute(
                    conn
                )
                return await query[int](t"SELECT sum(v)::int FROM mine").one(conn)
        finally:
            await asyncio.to_thread(instance.close)

    async def body() -> list[int]:
        return list(await asyncio.gather(*(one(index) for index in range(4))))

    expect(run(body), [index * 10_000 + 55 for index in range(4)])
    expect(leftovers(private_tmp), [])


# --- cleanup --------------------------------------------------------------------------------------------------


def test_an_instance_the_program_forgot_is_removed_at_exit(private_tmp: Path) -> None:
    code = "import rowx_embedded as e\ndb = e.Instance.start()\nprint(db.dsn)\n"
    result = python(code, private_tmp)
    expect(result.returncode, 0)
    expect_true(result.stdout.startswith("postgresql://"))
    expect(leftovers(private_tmp), [])


def test_a_dropped_instance_is_closed_without_blocking_the_caller(private_tmp: Path) -> None:
    instance = rowx_embedded.Instance.start()
    dsn = instance.dsn
    session = psycopg.connect(dsn, autocommit=True)

    def sleep() -> None:
        # closing the instance ends this session; the error is the expected outcome
        with contextlib.suppress(psycopg.OperationalError):
            session.execute("SELECT pg_sleep(2)")

    sleeper = threading.Thread(target=sleep)
    sleeper.start()
    time.sleep(0.3)
    started = time.monotonic()
    del instance
    gc.collect()
    # the engine thread is busy for another ~1.7 s; the destructor must not wait for it
    expect_true(time.monotonic() - started < 1.0)
    sleeper.join(timeout=STARTUP_WAIT)
    session.close()
    expect_true(wait_until(lambda: leftovers(private_tmp) == []))


def sleeping(dsn: str, seconds: int) -> threading.Thread:
    session = psycopg.connect(dsn, autocommit=True)

    def sleep() -> None:
        # abandoning the instance ends this session; the error is the expected outcome
        with contextlib.suppress(psycopg.Error):
            session.execute(f"SELECT pg_sleep({seconds})")
        with contextlib.suppress(psycopg.Error):
            session.close()

    thread = threading.Thread(target=sleep, daemon=True)
    thread.start()
    time.sleep(0.5)
    return thread


def test_abandon_during_a_running_statement_returns_at_once_and_removes_the_directory(private_tmp: Path) -> None:
    instance = rowx_embedded.Instance.start()
    directory = socket_dir(instance.dsn)
    sleeper = sleeping(instance.dsn, 20)
    started = time.monotonic()
    instance.abandon()
    expect_true(time.monotonic() - started < 1.0)
    expect_true(instance.closed)
    expect_false(directory.exists())
    expect(leftovers(private_tmp), [])
    # the clients are gone with the socket, though the engine is still inside its statement
    sleeper.join(timeout=STARTUP_WAIT)
    # closing afterwards does not wait for that statement either
    started = time.monotonic()
    instance.close()
    expect_true(time.monotonic() - started < 1.0)
    instance.abandon()


def test_abandon_then_close_and_close_then_abandon_are_idempotent(private_tmp: Path) -> None:
    first = rowx_embedded.Instance.start()
    first.abandon()
    first.close()
    first.abandon()
    second = rowx_embedded.Instance.start()
    second.close()
    second.abandon()
    expect_true(first.closed and second.closed)
    expect(leftovers(private_tmp), [])


def test_a_persistent_directory_is_not_abandoned_without_force(tmp_path: Path, private_tmp: Path) -> None:
    directory = tmp_path / "db"
    instance = rowx_embedded.Instance.start(str(directory))
    with pytest.raises(rowx_embedded.EngineError, match="cannot be abandoned"):
        instance.abandon()
    expect_false(instance.closed)
    expect(raw_one(instance.dsn, "SELECT 1"), 1)
    instance.close()
    expect(leftovers(private_tmp), [])


def test_a_forced_abandon_of_a_directory_mid_statement_leaves_data_that_opens_again(
    tmp_path: Path, private_tmp: Path
) -> None:
    directory = tmp_path / "db"
    code = (
        "import sys, threading, time, psycopg, rowx_embedded as e\n"
        "db = e.Instance.start(sys.argv[1])\n"
        "with psycopg.connect(db.dsn, autocommit=True) as c:\n"
        "    c.execute('CREATE TABLE kept (id int4)')\n"
        "    c.execute('INSERT INTO kept VALUES (7)')\n"
        "def sleep():\n"
        "    try:\n"
        "        psycopg.connect(db.dsn, autocommit=True).execute('SELECT pg_sleep(30)')\n"
        "    except psycopg.Error:\n"
        "        pass\n"
        "threading.Thread(target=sleep, daemon=True).start()\n"
        "time.sleep(1)\n"
        "db.abandon(force=True)\n"
    )
    started = time.monotonic()
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [sys.executable, "-c", code, str(directory)],
        env=child_env(private_tmp),
        capture_output=True,
        text=True,
        check=False,
        timeout=STARTUP_WAIT,
    )
    expect(result.returncode, 0)
    expect_true(time.monotonic() - started < ABANDON_LIMIT)
    expect(leftovers(private_tmp), [])
    reopened = rowx_embedded.Instance.start(str(directory))
    expect(raw_one(reopened.dsn, "SELECT id FROM kept"), 7)
    reopened.close()


def test_a_process_that_abandoned_an_instance_exits_without_waiting_for_its_statement(private_tmp: Path) -> None:
    code = (
        "import threading, time, psycopg, rowx_embedded as e\n"
        "db = e.Instance.start()\n"
        "def sleep():\n"
        "    try:\n"
        "        psycopg.connect(db.dsn, autocommit=True).execute('SELECT pg_sleep(30)')\n"
        "    except psycopg.Error:\n"
        "        pass\n"
        "threading.Thread(target=sleep, daemon=True).start()\n"
        "time.sleep(1)\n"
        "db.abandon()\n"
    )
    started = time.monotonic()
    result = python(code, private_tmp)
    expect(result.returncode, 0)
    # the atexit close of the engine must not wait for the statement that is still running
    expect_true(time.monotonic() - started < ABANDON_LIMIT)
    expect(leftovers(private_tmp), [])


ABANDON_RUNS: Final[int] = 20
STUCK_CLOSE: Final[str] = (
    "import threading, time, psycopg, rowx_embedded as e\n"
    "db = e.Instance.start()\n"
    "dsn = db.dsn\n"
    "def sleep():\n"
    "    try:\n"
    "        psycopg.connect(dsn, autocommit=True).execute('SELECT pg_sleep(30)')\n"
    "    except psycopg.Error:\n"
    "        pass\n"
    "threading.Thread(target=sleep, daemon=True).start()\n"
    "time.sleep(1)\n"
)


def abandoning(tail: str) -> str:
    return STUCK_CLOSE + tail


def test_a_normal_exit_after_an_abandon_never_crashes_the_process(private_tmp: Path) -> None:
    # the engine thread is still inside `pg_sleep` when the interpreter finalizes: it used to die by SIGSEGV (-11)
    code = abandoning("db.abandon()\n")
    with ThreadPoolExecutor(max_workers=4) as pool:
        codes = list(pool.map(lambda _: python(code, private_tmp).returncode, range(ABANDON_RUNS)))
    expect(codes, [0] * ABANDON_RUNS)
    expect(leftovers(private_tmp), [])


def test_an_abandoned_engine_keeps_the_exit_status_the_program_chose(private_tmp: Path) -> None:
    explicit = python(abandoning("db.abandon()\nimport rowx_embedded._api as a\na.exit_if_abandoned(3)\n"), private_tmp)
    expect(explicit.returncode, 3)
    # nothing abandoned: the explicit exit is a no-op and the program ends normally
    idle = python("import rowx_embedded._api as a\na.exit_if_abandoned(3)\n", private_tmp)
    expect(idle.returncode, 0)
    crashed = python(abandoning("db.abandon()\nraise RuntimeError('boom')\n"), private_tmp)
    expect(crashed.returncode, 1)
    expect_true("RuntimeError: boom" in crashed.stderr)
    recorded = python(abandoning("db.abandon()\nimport rowx_embedded._api as a\na.set_exit_code(4)\n"), private_tmp)
    expect(recorded.returncode, 4)


def test_the_exit_sweep_does_not_wait_for_a_background_close_stuck_on_a_long_statement(private_tmp: Path) -> None:
    # the instance is dropped (not closed) while a statement runs: its background close queues behind the statement
    code = STUCK_CLOSE + "del db\nimport gc\ngc.collect()\n"
    started = time.monotonic()
    result = python(code, private_tmp)
    expect(result.returncode, 0)
    expect_true(time.monotonic() - started < ABANDON_LIMIT)


def test_a_killed_process_leaves_a_root_that_the_next_start_sweeps(private_tmp: Path) -> None:
    code = (
        "import os, signal, sys\n"
        "import rowx_embedded as e\n"
        "db = e.Instance.start()\n"
        "print(db.dsn, flush=True)\n"
        "sys.stdin.readline()\n"
        "os.kill(os.getpid(), signal.SIGKILL)\n"
    )
    child = spawn(code, private_tmp)
    if child.stdout is None or child.stdin is None:
        pytest.fail("the child has no pipes")
    dsn = child.stdout.readline().strip()
    orphan = socket_dir(dsn)
    expect_true(orphan.exists())
    child.stdin.write("\n")
    child.stdin.flush()
    child.wait(timeout=STARTUP_WAIT)
    child.stdin.close()
    child.stdout.close()
    expect(child.returncode, -signal.SIGKILL)
    expect_true(orphan.exists())
    expect(leftovers(private_tmp), [orphan.name])

    survivor = rowx_embedded.Instance.start()
    try:
        expect_false(orphan.exists())
        expect(leftovers(private_tmp), [socket_dir(survivor.dsn).name])
    finally:
        survivor.close()
    expect(leftovers(private_tmp), [])


def test_a_live_foreign_root_is_not_swept(private_tmp: Path) -> None:
    # a root named for a live pid (ours) must survive another instance's sweep
    other = private_tmp / f"rxe-{os.getpid()}-zzzzzz"
    other.mkdir()
    rowx_embedded.Instance.start().close()
    expect_true(other.exists())


def test_a_forked_childs_close_never_touches_the_parents_instance(private_tmp: Path) -> None:
    instance = rowx_embedded.Instance.start()
    directory = socket_dir(instance.dsn)
    expect(raw_one(instance.dsn, "SELECT 1"), 1)
    with warnings.catch_warnings():
        # the process has engine threads; the child only closes and exits
        warnings.simplefilter("ignore", DeprecationWarning)
        pid = os.fork()
    if pid == 0:
        status = 1
        try:
            instance.close()
            rowx_embedded._engine.close_all()  # ruff: ignore[private-member-access]
            del instance
            gc.collect()
            status = 0 if directory.exists() else 2
        finally:
            os._exit(status)
    _, wait_status = os.waitpid(pid, 0)
    expect(os.waitstatus_to_exitcode(wait_status), 0)
    expect_true(directory.exists())
    expect_false(instance.closed)
    expect(raw_one(instance.dsn, "SELECT 2"), 2)
    instance.close()
    expect_false(directory.exists())
    with pytest.raises(rowx_embedded.EngineError):
        instance.snapshot()
    expect(leftovers(private_tmp), [])


# --- a raw wire client ----------------------------------------------------------------------------------------
# the proxy is tested below the driver too: some of what it must survive no real client sends


PROTOCOL_3: Final[int] = 196_608
READY_FOR_QUERY: Final[bytes] = b"Z"
READ_BYTES: Final[int] = 65_536
SHORT_TIMEOUT: Final[float] = 10.0


def frame(tag: bytes, body: bytes = b"") -> bytes:
    return tag + struct.pack("!i", len(body) + 4) + body


def startup_packet(params: dict[str, str], version: int = PROTOCOL_3) -> bytes:
    body = struct.pack("!i", version)
    for key, value in params.items():
        body += key.encode() + b"\0" + value.encode() + b"\0"
    body += b"\0"
    return struct.pack("!i", len(body) + 4) + body


def simple(sql: str) -> bytes:
    return frame(b"Q", sql.encode() + b"\0")


def parse_frame(sql: str, name: bytes = b"") -> bytes:
    return frame(b"P", name + b"\0" + sql.encode() + b"\0" + struct.pack("!h", 0))


def bind_frame(portal: bytes = b"", statement: bytes = b"") -> bytes:
    return frame(b"B", portal + b"\0" + statement + b"\0" + struct.pack("!hhh", 0, 0, 0))


def execute_frame(portal: bytes = b"") -> bytes:
    return frame(b"E", portal + b"\0" + struct.pack("!i", 0))


SYNC: Final[bytes] = frame(b"S")
TERMINATE: Final[bytes] = frame(b"X")


class Wire:
    def __init__(self, dsn: str, params: dict[str, str] | None = None, *, handshake: bool = True) -> None:
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(SHORT_TIMEOUT)
        self.sock.connect(str(socket_dir(dsn) / SOCKET_NAME))
        self.buffer = b""
        if handshake:
            self.sock.sendall(startup_packet(params or {"user": "postgres", "database": "postgres"}))
            self.until_ready()

    def close(self) -> None:
        self.sock.close()

    def fill(self) -> None:
        data = self.sock.recv(READ_BYTES)
        if not data:
            raise EOFError
        self.buffer += data

    def message(self) -> tuple[bytes, bytes]:
        while len(self.buffer) < 5:  # ruff: ignore[magic-value-comparison]
            self.fill()
        (length,) = struct.unpack("!i", self.buffer[1:5])
        while len(self.buffer) < 1 + length:
            self.fill()
        tag, body = self.buffer[:1], self.buffer[5 : 1 + length]
        self.buffer = self.buffer[1 + length :]
        return tag, body

    def until_ready(self) -> list[tuple[bytes, bytes]]:
        out: list[tuple[bytes, bytes]] = []
        while True:
            message = self.message()
            out.append(message)
            if message[0] == READY_FOR_QUERY:
                return out

    def send(self, *frames: bytes) -> None:
        self.sock.sendall(b"".join(frames))

    def query(self, sql: str) -> list[list[str | None]]:
        self.send(simple(sql))
        out = self.until_ready()
        errors = [body for tag, body in out if tag == b"E"]
        if errors:
            pytest.fail(f"{sql!r} failed: {errors[0]!r}")
        return rows_of(out)


def rows_of(messages: list[tuple[bytes, bytes]]) -> list[list[str | None]]:
    rows: list[list[str | None]] = []
    for tag, body in messages:
        if tag != b"D":
            continue
        (count,) = struct.unpack("!h", body[:2])
        at = 2
        row: list[str | None] = []
        for _ in range(count):
            (length,) = struct.unpack("!i", body[at : at + 4])
            at += 4
            row.append(None if length < 0 else body[at : at + length].decode())
            at += max(length, 0)
        rows.append(row)
    return rows


def error_code(body: bytes) -> str:
    for field in body.split(b"\0"):
        if field.startswith(b"C"):
            return field[1:].decode()
    return ""


def next_session_works(dsn: str) -> bool:
    wire = Wire(dsn)
    try:
        return wire.query("SELECT 1") == [["1"]]
    finally:
        wire.close()


# --- COPY ------------------------------------------------------------------------------------------------------

COPY_ROWS: Final[int] = 20_000


def test_copy_from_stdin_and_to_stdout_work_through_psycopg(db: Instance) -> None:
    with psycopg.connect(db.dsn, autocommit=True) as conn:
        conn.execute("CREATE TABLE t (i int, s text)")
        with conn.cursor().copy("COPY t FROM STDIN") as copy:
            for i in range(COPY_ROWS):
                copy.write_row((i, f"row {i}"))
        expect(conn.execute("SELECT count(*), sum(i) FROM t").fetchone(), (COPY_ROWS, sum(range(COPY_ROWS))))
        with conn.cursor().copy("COPY (SELECT i FROM t ORDER BY i LIMIT 3) TO STDOUT") as copy:
            expect(b"".join(bytes(chunk) for chunk in copy), b"0\n1\n2\n")
    # the engine is still sound for the next session
    expect(raw_one(db.dsn, "SELECT count(*) FROM t"), COPY_ROWS)


def test_copy_from_stdin_is_a_part_of_the_clients_transaction(db: Instance) -> None:
    with psycopg.connect(db.dsn) as conn:
        conn.execute("CREATE TABLE t (i int)")
        conn.commit()
        with conn.cursor().copy("COPY t FROM STDIN") as copy:
            copy.write_row((1,))
        expect(conn.execute("SELECT count(*) FROM t").fetchone(), (1,))
        conn.rollback()
        expect(conn.execute("SELECT count(*) FROM t").fetchone(), (0,))
        with conn.cursor().copy("COPY t FROM STDIN") as copy:
            copy.write_row((2,))
        conn.commit()
    expect(raw_one(db.dsn, "SELECT i FROM t"), 2)


def test_a_copy_that_fails_keeps_the_session_and_the_transaction_usable(db: Instance) -> None:
    with psycopg.connect(db.dsn) as conn:
        conn.execute("CREATE TABLE t (i int)")
        conn.commit()
        conn.execute("INSERT INTO t VALUES (1)")
        with pytest.raises(psycopg.errors.InvalidTextRepresentation), conn.cursor().copy("COPY t FROM STDIN") as copy:
            copy.write(b"not a number\n")
        conn.rollback()
        expect(conn.execute("SELECT count(*) FROM t").fetchone(), (0,))
        with pytest.raises(psycopg.errors.UndefinedTable), conn.cursor().copy("COPY missing FROM STDIN"):
            pass
        conn.rollback()
        expect(conn.execute("SELECT 1").fetchone(), (1,))
    expect_true(next_session_works(db.dsn))


def test_a_failed_copy_inside_a_transaction_does_not_end_it_early(db: Instance) -> None:
    wire = Wire(db.dsn)
    try:
        wire.query("CREATE TABLE t (i int)")
        wire.query("BEGIN")
        wire.query("INSERT INTO t VALUES (7)")
        wire.send(simple("COPY t FROM STDIN"))
        tag, _ = wire.message()
        expect(tag, b"G")
        wire.send(frame(b"f", b"changed my mind\0"))
        out = wire.until_ready()
        expect(out[-1], (READY_FOR_QUERY, b"E"))
        expect(error_code(next(body for tag, body in out if tag == b"E")), "57014")
        wire.query("ROLLBACK")
        expect(wire.query("SELECT count(*) FROM t"), [["0"]])
    finally:
        wire.close()


def test_a_client_that_leaves_in_the_middle_of_a_copy_changes_nothing(db: Instance) -> None:
    wire = Wire(db.dsn)
    wire.query("CREATE TABLE t (i int)")
    wire.send(simple("COPY t FROM STDIN"))
    expect(wire.message()[0], b"G")
    wire.send(frame(b"d", b"5\n"))
    wire.close()
    wire = Wire(db.dsn)
    try:
        expect(wire.query("SELECT count(*) FROM t"), [["0"]])
        expect(wire.query("SELECT 1"), [["1"]])
    finally:
        wire.close()


def test_copy_from_stdin_through_the_extended_protocol_is_refused_cleanly(db: Instance) -> None:
    wire = Wire(db.dsn)
    try:
        wire.query("CREATE TABLE t (i int)")
        wire.send(parse_frame("COPY t FROM STDIN"), bind_frame(), execute_frame(), SYNC)
        out = wire.until_ready()
        expect(error_code(next(body for tag, body in out if tag == b"E")), "0A000")
        expect(out[-1], (READY_FOR_QUERY, b"I"))
        expect(wire.query("SELECT 1"), [["1"]])
    finally:
        wire.close()


# --- frames the engine cannot take ----------------------------------------------------------------------------

REFUSED_FRAMES: Final[dict[str, bytes]] = {
    "unknown tag": frame(b"?", b"zz"),
    "password message": frame(b"p", b"x\0"),
    "function call": frame(b"F", b"\0\0\0\0"),
    "copy data outside a copy": frame(b"d", b"hi"),
    "copy done outside a copy": frame(b"c"),
    "query without a terminator": frame(b"Q", b"select 1"),
    "empty query message": frame(b"Q"),
    "bind with nothing": frame(b"B", b"x"),
    "describe garbage": frame(b"D", b"Zfoo\0"),
    "execute an unknown portal": execute_frame(b"nope"),
}


@pytest.mark.parametrize("name", list(REFUSED_FRAMES))
def test_an_unsupported_or_malformed_frame_never_wedges_the_engine(db: Instance, name: str) -> None:
    wire = Wire(db.dsn)
    wire.send(REFUSED_FRAMES[name], SYNC)
    wire.sock.settimeout(2)
    # whatever the client gets back (an error, or nothing for frames a server ignores), it is never a silent EOF
    with contextlib.suppress(TimeoutError):
        wire.message()
    wire.close()
    expect_true(next_session_works(db.dsn))
    expect(raw_one(db.dsn, "SELECT 1"), 1)


def test_an_unknown_frame_gets_a_fatal_error_not_a_silent_eof(db: Instance) -> None:
    wire = Wire(db.dsn)
    try:
        wire.send(frame(b"?", b"zz"))
        tag, body = wire.message()
        expect(tag, b"E")
        expect(error_code(body), "08P01")
    finally:
        wire.close()


def test_an_oversized_or_negative_frame_length_is_answered_with_an_error(db: Instance) -> None:
    for length in (3, -5, 0x7FFFFFFF):
        wire = Wire(db.dsn)
        try:
            wire.send(b"Q" + struct.pack("!i", length))
            tag, body = wire.message()
            expect((tag, error_code(body)), (b"E", "08P01"))
        finally:
            wire.close()
    expect_true(next_session_works(db.dsn))


def test_the_startup_can_arrive_a_byte_at_a_time_and_pipelined_with_a_query(db: Instance) -> None:
    wire = Wire(db.dsn, handshake=False)
    try:
        for byte in startup_packet({"user": "postgres", "database": "postgres"}):
            wire.sock.sendall(bytes([byte]))
        wire.until_ready()
        for byte in simple("SELECT 1 + 1"):
            wire.sock.sendall(bytes([byte]))
        expect(rows_of(wire.until_ready()), [["2"]])
    finally:
        wire.close()
    wire = Wire(db.dsn, handshake=False)
    try:
        wire.sock.sendall(
            struct.pack("!ii", 8, SSL_REQUEST)
            + startup_packet({"user": "postgres", "database": "postgres"})
            + simple("SELECT 7"),
        )
        expect(wire.sock.recv(1), b"N")
        expect(rows_of(wire.until_ready() + wire.until_ready()), [["7"]])
    finally:
        wire.close()


def test_a_newer_protocol_minor_version_is_negotiated_down(db: Instance) -> None:
    wire = Wire(db.dsn, handshake=False)
    try:
        wire.sock.sendall(startup_packet({"user": "postgres", "database": "postgres"}, PROTOCOL_3 + 2))
        tag, body = wire.message()
        expect(tag, b"v")
        expect(struct.unpack("!ii", body[:8]), (0, 0))
        wire.until_ready()
        expect(wire.query("SELECT 1"), [["1"]])
    finally:
        wire.close()


def test_unknown_protocol_options_are_listed_in_the_negotiation(db: Instance) -> None:
    wire = Wire(db.dsn, handshake=False)
    try:
        wire.sock.sendall(startup_packet({"user": "postgres", "database": "postgres", "_pq_.thing": "1"}))
        tag, body = wire.message()
        expect(tag, b"v")
        expect_true(body.endswith(b"_pq_.thing\0"))
        wire.until_ready()
        expect(wire.query("SELECT 1"), [["1"]])
    finally:
        wire.close()


def test_a_different_protocol_major_version_is_refused_with_an_error(db: Instance) -> None:
    wire = Wire(db.dsn, handshake=False)
    try:
        wire.sock.sendall(startup_packet({"user": "postgres"}, 4 << 16))
        tag, body = wire.message()
        expect((tag, error_code(body)), (b"E", "0A000"))
    finally:
        wire.close()


# --- pipelining ------------------------------------------------------------------------------------------------


def test_a_pipelined_batch_is_never_left_unsent_while_the_client_waits(db: Instance) -> None:
    wire = Wire(db.dsn)
    try:
        # [Parse Bind Execute Sync][Parse Bind Execute] in one write, as libpq pipeline mode sends them
        wire.send(
            parse_frame("select 1"),
            bind_frame(),
            execute_frame(),
            SYNC,
            parse_frame("select 2"),
            bind_frame(),
            execute_frame(),
        )
        expect(rows_of(wire.until_ready()), [["1"]])
    finally:
        wire.close()


def test_psycopg_pipeline_mode_works_and_a_reset_inside_it_takes_effect_before_the_next_statement(db: Instance) -> None:
    dsn = f"{db.dsn}&options=-c%20work_mem%3D5MB"
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("SET work_mem = '9MB'")
        with conn.pipeline():
            first = conn.execute("SELECT generate_series(1, 3)")
            conn.execute("RESET ALL")
            shown = conn.execute("SHOW work_mem")
            last = conn.execute("SELECT 42")
        expect(first.fetchall(), [(1,), (2,), (3,)])
        expect(shown.fetchall(), [("5MB",)])
        expect(last.fetchall(), [(42,)])


def pipeline_with_an_error(conn: psycopg.Connection) -> None:
    with conn.pipeline():
        conn.execute("SELECT 1")
        conn.execute("SELECT 1 / 0")
        conn.execute("SELECT 2")


def test_a_pipelined_batch_with_an_error_in_the_middle_resyncs(db: Instance) -> None:
    with psycopg.connect(db.dsn, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.DivisionByZero):
            pipeline_with_an_error(conn)
        expect(conn.execute("SELECT 3").fetchone(), (3,))
    expect_true(next_session_works(db.dsn))


# --- RESET / DISCARD detection ---------------------------------------------------------------------------------

STARTUP_WORK_MEM: Final[str] = "5MB"
CLIENT_WORK_MEM: Final[str] = "9MB"


@pytest.mark.parametrize(
    ("statements", "expected"),
    [
        ((), CLIENT_WORK_MEM),
        (("SELECT 'reset '",), CLIENT_WORK_MEM),
        (("SELECT 1 AS preset",), CLIENT_WORK_MEM),
        (("SELECT 1 /* discard all */",), CLIENT_WORK_MEM),
        (("SELECT 1 -- reset all",), CLIENT_WORK_MEM),
        (("SELECT $$ reset all $$",), CLIENT_WORK_MEM),
        (("SET LOCAL work_mem TO DEFAULT",), CLIENT_WORK_MEM),
        (("RESET ALL",), STARTUP_WORK_MEM),
        (("RESET\nALL",), STARTUP_WORK_MEM),
        (("RESET\tALL",), STARTUP_WORK_MEM),
        (("DISCARD\nALL",), STARTUP_WORK_MEM),
        (("/* hidden */ RESET ALL",), STARTUP_WORK_MEM),
        (("RESET work_mem",), STARTUP_WORK_MEM),
        (("SET work_mem TO DEFAULT",), STARTUP_WORK_MEM),
        (("SET work_mem = DEFAULT",), STARTUP_WORK_MEM),
        (("SET work_mem = '11MB'; RESET ALL",), STARTUP_WORK_MEM),
    ],
)
def test_only_real_resets_restore_the_startup_options(statements: tuple[str, ...], expected: str, db: Instance) -> None:
    dsn = f"{db.dsn}&options=-c%20work_mem%3D{STARTUP_WORK_MEM}"
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f"SET work_mem = '{CLIENT_WORK_MEM}'")
        for statement in statements:
            conn.execute(statement)
        expect(conn.execute("SHOW work_mem").fetchone(), (expected,))


def test_a_reset_through_a_prepared_statement_restores_the_startup_options(db: Instance) -> None:
    dsn = f"{db.dsn}&options=-c%20work_mem%3D{STARTUP_WORK_MEM}"
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f"SET work_mem = '{CLIENT_WORK_MEM}'")
        conn.execute("RESET ALL", prepare=True)
        expect(conn.execute("SHOW work_mem").fetchone(), (STARTUP_WORK_MEM,))


def test_a_reset_in_the_extended_protocol_with_two_syncs_in_one_write(db: Instance) -> None:
    wire = Wire(db.dsn, {"user": "postgres", "database": "postgres", "options": "-c work_mem=5MB"})
    try:
        wire.query("SET work_mem = '9MB'")
        wire.send(
            parse_frame("RESET ALL"),
            bind_frame(),
            execute_frame(),
            SYNC,
            parse_frame("SHOW work_mem"),
            bind_frame(),
            execute_frame(),
            SYNC,
        )
        wire.until_ready()
        expect(rows_of(wire.until_ready()), [["5MB"]])
    finally:
        wire.close()


# --- a client that leaves in an awkward state --------------------------------------------------------------------


def test_a_client_that_leaves_with_an_unsynced_extended_error_does_not_poison_the_next_session(db: Instance) -> None:
    for sql, in_transaction in (("selec 1", False), ("select 1/0", True)):
        wire = Wire(db.dsn)
        if in_transaction:
            wire.query("BEGIN")
        wire.send(parse_frame(sql), bind_frame(), execute_frame(), frame(b"H"))
        wire.message()
        wire.close()
        wire = Wire(db.dsn)
        try:
            expect(wire.query("SELECT 1"), [["1"]])
            expect(wire.query("SELECT txid_current_if_assigned()"), [[None]])
        finally:
            wire.close()


def test_a_client_that_leaves_inside_a_transaction_changes_nothing(db: Instance) -> None:
    wire = Wire(db.dsn)
    wire.query("CREATE TABLE t (i int)")
    wire.query("BEGIN")
    wire.query("INSERT INTO t VALUES (1)")
    wire.close()
    wire = Wire(db.dsn)
    try:
        expect(wire.query("SELECT count(*) FROM t"), [["0"]])
        wire.query("BEGIN")
        wire.query("ROLLBACK")
    finally:
        wire.close()


def test_a_client_that_leaves_without_terminate_never_makes_the_next_connect_fail(db: Instance) -> None:
    for action in (None, simple("select 1"), simple("select pg_sleep(0.05)")):
        for _ in range(100):
            wire = Wire(db.dsn)
            if action is not None:
                wire.send(action)
            wire.close()


def test_a_client_that_leaves_with_a_long_query_running_is_waited_for_not_refused(db: Instance) -> None:
    wire = Wire(db.dsn)
    wire.send(simple("SELECT pg_sleep(1.5)"))
    time.sleep(0.2)
    wire.close()
    started = time.monotonic()
    # the engine cannot interrupt the statement: the next client waits for it rather than being refused with 53300
    expect(raw_one(db.dsn, "SELECT 1"), 1)
    expect_true(time.monotonic() - started > WAITED_FOR_A_QUERY)


def test_a_live_second_client_is_still_refused_while_the_first_waits_on_a_slow_query(db: Instance) -> None:
    wire = Wire(db.dsn)
    wire.send(simple("SELECT pg_sleep(1)"))
    time.sleep(0.2)
    try:
        with pytest.raises(psycopg.OperationalError, match="too many clients"):
            psycopg.connect(db.dsn)
        wire.until_ready()
    finally:
        wire.close()


LARGE_ROWS: Final[int] = 100_000


def test_a_large_result_set_streams_through_the_proxy(db: Instance) -> None:
    with psycopg.connect(db.dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT count(*), sum(length(s)) FROM (SELECT repeat('x', 100) AS s FROM generate_series(1, %s)) AS t",
            (LARGE_ROWS,),
        ).fetchone()
        expect(row, (LARGE_ROWS, LARGE_ROWS * 100))
        fetched = conn.execute("SELECT repeat('y', 100) FROM generate_series(1, %s)", (LARGE_ROWS,)).fetchall()
        expect(len(fetched), LARGE_ROWS)

    async def body() -> int:
        async with await rowx_postgres.connect(db.dsn) as conn:
            return len(await query[str](t"SELECT repeat('z', 100) FROM generate_series(1, {LARGE_ROWS})").all(conn))

    expect(run(body), LARGE_ROWS)


def test_a_client_that_stops_reading_a_large_result_does_not_block_the_next_one(db: Instance) -> None:
    wire = Wire(db.dsn)
    wire.send(simple("SELECT repeat('x', 1000) FROM generate_series(1, 300000)"))
    wire.message()
    wire.close()
    expect(raw_one(db.dsn, "SELECT 1"), 1)


# --- closing ---------------------------------------------------------------------------------------------------


def run_in_thread(target: Callable[[], object]) -> threading.Thread:
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


def test_close_does_not_depend_on_the_socket_file_still_existing(private_tmp: Path) -> None:
    instance = rowx_embedded.Instance.start()
    directory = socket_dir(instance.dsn)
    (directory / SOCKET_NAME).unlink()
    closer = run_in_thread(instance.close)
    closer.join(SHORT_TIMEOUT)
    expect_false(closer.is_alive())
    expect_false(directory.exists())
    expect(leftovers(private_tmp), [])


def test_close_ends_an_idle_connected_client(private_tmp: Path) -> None:
    instance = rowx_embedded.Instance.start()
    conn = psycopg.connect(instance.dsn, autocommit=True)
    conn.execute("SELECT 1")
    instance.close()
    with pytest.raises(psycopg.OperationalError):
        conn.execute("SELECT 1")
    conn.close()
    expect(leftovers(private_tmp), [])


def test_close_during_a_long_query_waits_for_it_and_cleans_up(private_tmp: Path) -> None:
    instance = rowx_embedded.Instance.start()
    session = psycopg.connect(instance.dsn, autocommit=True)

    def sleep() -> None:
        with contextlib.suppress(psycopg.OperationalError):
            session.execute("SELECT pg_sleep(1.5)")

    sleeper = run_in_thread(sleep)
    time.sleep(0.3)
    started = time.monotonic()
    instance.close()
    expect_true(time.monotonic() - started > WAITED_FOR_A_QUERY)
    sleeper.join(STARTUP_WAIT)
    session.close()
    expect(leftovers(private_tmp), [])


def test_a_second_concurrent_close_returns_only_when_the_first_has_finished(private_tmp: Path) -> None:
    instance = rowx_embedded.Instance.start()
    directory = socket_dir(instance.dsn)
    session = psycopg.connect(instance.dsn, autocommit=True)

    def sleep() -> None:
        with contextlib.suppress(psycopg.OperationalError):
            session.execute("SELECT pg_sleep(1.5)")

    sleeper = run_in_thread(sleep)
    time.sleep(0.3)
    first = run_in_thread(instance.close)
    time.sleep(0.3)
    # the first close is mid-way (waiting for the statement); the second must wait for it, not return early
    instance.close()
    expect_false(directory.exists())
    first.join(STARTUP_WAIT)
    sleeper.join(STARTUP_WAIT)
    session.close()
    expect(leftovers(private_tmp), [])


def test_closing_twice_from_many_threads_is_harmless(private_tmp: Path) -> None:
    instance = rowx_embedded.Instance.start()
    threads = [run_in_thread(instance.close) for _ in range(6)]
    for thread in threads:
        thread.join(STARTUP_WAIT)
    expect_false(any(thread.is_alive() for thread in threads))
    expect(leftovers(private_tmp), [])


def test_a_relative_tmpdir_still_gives_an_absolute_socket_directory() -> None:
    # a very short working directory: the resolved socket path is limited to about 100 bytes
    cwd = Path(tempfile.mkdtemp(prefix="rxr-", dir="/tmp"))
    try:
        (cwd / "rel").mkdir()
        code = (
            "import rowx_embedded as e\n"
            "db = e.Instance.start()\n"
            "print(db.dsn.split('host=')[1].split('&')[0])\n"
            "db.close()\n"
        )
        result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
            [sys.executable, "-c", code],
            env={**os.environ, "TMPDIR": "rel"},
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=STARTUP_WAIT,
            check=False,
        )
        expect(result.returncode, 0)
        expect_true(Path(result.stdout.strip()).is_absolute())
    finally:
        shutil.rmtree(cwd, ignore_errors=True)


# --- seeds, snapshots and directories -----------------------------------------------------------------------------


def test_a_corrupted_snapshot_is_refused_even_once_its_seed_is_cached(private_tmp: Path) -> None:
    source = rowx_embedded.Instance.start()
    with psycopg.connect(source.dsn, autocommit=True) as conn:
        conn.execute("CREATE TABLE t (i int)")
    snapshot = source.snapshot()
    source.close()
    corrupted = bytearray(snapshot)
    corrupted[-100] ^= 0xFF
    good = rowx_embedded.Instance.start(snapshot=snapshot)
    expect(good.boot, "seed")
    good.close()
    # the seed for this snapshot is now cached; a spliced payload under the same header must still be caught
    with pytest.raises(rowx_embedded.SnapshotError, match="digest"):
        rowx_embedded.Instance.start(snapshot=bytes(corrupted))
    expect(leftovers(private_tmp), [])


def test_when_the_seed_path_fails_the_snapshot_boots_through_a_directory_restore(
    monkeypatch: pytest.MonkeyPatch,
    private_tmp: Path,
) -> None:
    source = rowx_embedded.Instance.start()
    seed_data(source.dsn)
    snapshot = source.snapshot()
    source.close()
    monkeypatch.setenv("ROWX_EMBEDDED_TEST_FAIL_SEED", "1")
    restored = rowx_embedded.Instance.start(snapshot=snapshot)
    expect(restored.boot, "restore")
    expect(raw_one(restored.dsn, "SELECT count(*) FROM people"), 50)
    expect(raw_one(restored.dsn, "SHOW fsync"), "off")
    restored.close()
    expect(leftovers(private_tmp), [])


def test_restoring_a_snapshot_into_a_directory_in_use_names_the_owner_and_harms_nothing(tmp_path: Path) -> None:
    source = rowx_embedded.Instance.start()
    snapshot = source.snapshot()
    source.close()
    directory = str(tmp_path / "pg")
    live = rowx_embedded.Instance.start(directory)
    try:
        with psycopg.connect(live.dsn, autocommit=True) as conn:
            conn.execute("CREATE TABLE precious (i int)")
            conn.execute("INSERT INTO precious VALUES (42)")
        with pytest.raises(rowx_embedded.DirectoryInUseError) as caught:
            rowx_embedded.Instance.start(directory, snapshot)
        expect(caught.value.pid, os.getpid())
        expect(raw_one(live.dsn, "SELECT i FROM precious"), 42)
    finally:
        live.close()


def test_a_stale_owner_file_never_names_a_dead_pid(tmp_path: Path) -> None:
    directory = tmp_path / "pg"
    holder = rowx_embedded.Instance.start(str(directory))
    try:
        (tmp_path / ".pg.rowx-owner").write_text(f"{2**22 - 3}\n")
        with pytest.raises(rowx_embedded.DirectoryInUseError, match="in use by another instance") as caught:
            rowx_embedded.Instance.start(str(directory))
        expect(caught.value.pid, None)
    finally:
        holder.close()


def test_the_owner_file_is_never_written_through_a_symlink(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.write_text("precious")
    (tmp_path / ".pg.rowx-owner").symlink_to(victim)
    instance = rowx_embedded.Instance.start(str(tmp_path / "pg"))
    instance.close()
    expect(victim.read_text(), "precious")


def test_a_locked_root_survives_the_sweep_even_when_its_pid_looks_dead(private_tmp: Path) -> None:
    # as if a process in another pid namespace, sharing this TMPDIR, owned it
    foreign = private_tmp / f"rxe-{2**22 - 5}-foreign"
    foreign.mkdir()
    with (foreign / ".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        rowx_embedded.Instance.start().close()
        expect_true(foreign.exists())
    rowx_embedded.Instance.start().close()
    expect_false(foreign.exists())


# --- the host survives the engine's error paths --------------------------------------------------------------------


def test_postgres_error_paths_do_not_crash_the_host(db: Instance) -> None:
    # these paths raise through the wasm unwinder; on macOS arm64 the stock wasmer-compiler killed the process here
    statements = [
        "CREATE TABLE u (i int PRIMARY KEY)",
        "INSERT INTO u VALUES (1)",
    ]
    with psycopg.connect(db.dsn, autocommit=True) as conn:
        for statement in statements:
            conn.execute(statement)
        for _ in range(20):
            with pytest.raises(psycopg.errors.UniqueViolation):
                conn.execute("INSERT INTO u VALUES (1)")
            with pytest.raises(psycopg.errors.RaiseException):
                conn.execute("DO $$ BEGIN RAISE EXCEPTION 'boom'; END $$")
            with pytest.raises(psycopg.errors.DivisionByZero):
                conn.execute("SELECT 1 / 0")
            with pytest.raises(psycopg.errors.InvalidTextRepresentation):
                conn.execute("SELECT 'x'::int")
        expect(conn.execute("SELECT count(*) FROM u").fetchone(), (1,))
