import asyncio
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from rowx_core.errors import InterfaceError
from rowx_postgres import _embedded_testing as module
from rowx_postgres._embedded import SnapshotRejectedError
from rowx_postgres._embedded_testing import (
    EmbeddedProvisioner,
    lock,
    publish_blocking,
    read_verified,
    remove_stale_blocking,
    snapshot_path,
    try_lock,
    unlock,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

PROJECT: Final[str] = "0a1b2c3d"
NAME: Final[str] = f"rowx_tpl_{PROJECT}_00112233445566ff"


def test_the_snapshot_file_is_named_by_the_project_and_the_hash(tmp_path: Path) -> None:
    assert snapshot_path(tmp_path, PROJECT, NAME) == tmp_path / f"{PROJECT}_00112233445566ff.snapshot"


def test_a_published_snapshot_verifies_and_leaves_no_temporary_file(tmp_path: Path) -> None:
    path = snapshot_path(tmp_path, PROJECT, NAME)
    (tmp_path / f"{PROJECT}_dead.snapshot.1.ab.tmp").write_bytes(b"a build that died")
    (tmp_path / "other_dead.snapshot.1.ab.tmp").write_bytes(b"another project's")
    publish_blocking(path, PROJECT, b"snapshot bytes")
    assert read_verified(path) == b"snapshot bytes"
    assert sorted(entry.name for entry in tmp_path.iterdir()) == [path.name, "other_dead.snapshot.1.ab.tmp"]


def test_a_failed_publish_leaves_nothing_and_keeps_the_old_snapshot(tmp_path: Path) -> None:
    path = snapshot_path(tmp_path, PROJECT, NAME)
    publish_blocking(path, PROJECT, b"first")
    with pytest.raises(TypeError):
        publish_blocking(path, PROJECT, "not bytes")  # ty: ignore[invalid-argument-type]
    assert read_verified(path) == b"first"
    assert [entry.name for entry in tmp_path.iterdir()] == [path.name]


def test_a_missing_short_truncated_or_altered_snapshot_does_not_verify(tmp_path: Path) -> None:
    path = snapshot_path(tmp_path, PROJECT, NAME)
    assert read_verified(path) is None
    publish_blocking(path, PROJECT, b"x" * 1000)
    good = path.read_bytes()
    path.write_bytes(b"")
    assert read_verified(path) is None
    path.write_bytes(good[:10])
    assert read_verified(path) is None
    path.write_bytes(good[:-1])
    assert read_verified(path) is None
    flipped = bytearray(good)
    flipped[500] ^= 1
    path.write_bytes(bytes(flipped))
    assert read_verified(path) is None
    # an altered digest does not verify either
    path.write_bytes(good[:-1] + bytes([good[-1] ^ 1]))
    assert read_verified(path) is None
    path.write_bytes(good)
    assert read_verified(path) == b"x" * 1000


def test_stale_cleanup_removes_only_this_projects_other_snapshots(tmp_path: Path) -> None:
    keep = snapshot_path(tmp_path, PROJECT, NAME)
    names = [keep.name, f"{PROJECT}_old.snapshot", "ffffffff_0123.snapshot", f"{PROJECT}.lock", f"{PROJECT}_x.txt"]
    for name in names:
        (tmp_path / name).write_bytes(b"x")
    remove_stale_blocking(keep, PROJECT)
    assert sorted(entry.name for entry in tmp_path.iterdir()) == sorted([
        keep.name,
        "ffffffff_0123.snapshot",
        f"{PROJECT}.lock",
        f"{PROJECT}_x.txt",
    ])


def test_the_lock_is_exclusive_until_released(tmp_path: Path) -> None:
    path = tmp_path / "p.lock"
    first = try_lock(path)
    assert first is not None
    assert try_lock(path) is None
    unlock(first)
    second = try_lock(path)
    assert second is not None
    unlock(second)


def entries(path: Path) -> int:
    return sum(1 for _ in path.iterdir())


def open_descriptors() -> int:
    return sum(1 for _ in Path("/dev/fd").iterdir())


def test_a_cancelled_wait_for_the_lock_leaks_no_descriptor(tmp_path: Path) -> None:
    path = tmp_path / "p.lock"
    held = try_lock(path)
    assert held is not None

    async def scenario() -> None:
        before = open_descriptors()
        waiting = asyncio.create_task(lock(path))
        await asyncio.sleep(0.2)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert open_descriptors() == before

    asyncio.run(scenario())
    unlock(held)
    # the lock was never taken by the cancelled waiter
    again = try_lock(path)
    assert again is not None
    unlock(again)


def test_a_waiting_lock_is_taken_once_released(tmp_path: Path) -> None:
    path = tmp_path / "p.lock"
    held = try_lock(path)
    assert held is not None

    async def scenario() -> int:
        waiting = asyncio.create_task(lock(path))
        await asyncio.sleep(0.1)
        assert not waiting.done()
        unlock(held)
        return await waiting

    unlock(asyncio.run(scenario()))


@dataclass(slots=True)
class FakeDatabase:
    dsn: str = "postgresql://postgres@/postgres?host=/x"
    closed: bool = False
    abandoned: int = 0

    def abandon(self) -> None:
        self.abandoned += 1
        self.closed = True


@dataclass(slots=True)
class Fakes:
    builds: int = 0
    boots: list[bytes] = field(default_factory=list)
    # what each boot does, in order; an exception is raised and anything else is a database
    script: list[BaseException | None] = field(default_factory=list)
    databases: list[FakeDatabase] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> Fakes:
    state = Fakes()

    async def build(migrator: object, query: str) -> bytes:
        await asyncio.sleep(0)
        state.builds += 1
        state.queries.append(f"{migrator is None}:{query}")
        return f"built {state.builds}".encode()

    async def boot(snapshot: bytes, *, directory: str | None = None) -> FakeDatabase:
        await asyncio.sleep(0)
        assert directory is None
        state.boots.append(snapshot)
        if state.script and (step := state.script.pop(0)) is not None:
            raise step
        database = FakeDatabase()
        state.databases.append(database)
        return database

    monkeypatch.setattr(module, "build_snapshot", build)
    monkeypatch.setattr(module, "start_from_snapshot", boot)
    monkeypatch.setattr(module, "identity", lambda: ("engine:test",))
    return state


def provisioner(tmp_path: Path, url: str = "embedded://") -> EmbeddedProvisioner:
    return EmbeddedProvisioner(url, cache=lambda _: tmp_path)


def run[T](work: Callable[[], Awaitable[T]]) -> T:
    return asyncio.run(work())


def test_a_good_cached_snapshot_is_reused_without_a_build_or_a_boot(tmp_path: Path, fakes: Fakes) -> None:
    publish_blocking(snapshot_path(tmp_path, PROJECT, NAME), PROJECT, b"cached")

    async def scenario() -> None:
        target = provisioner(tmp_path)
        hold = await target.ensure_template(None, NAME, keep_stale=False, project=PROJECT)
        await hold.close()

    run(scenario)
    assert fakes.builds == 0
    assert fakes.boots == []


@pytest.mark.parametrize("damage", ["missing", "altered", "empty"])
def test_a_missing_or_damaged_snapshot_is_built_and_published(tmp_path: Path, fakes: Fakes, damage: str) -> None:
    path = snapshot_path(tmp_path, PROJECT, NAME)
    if damage != "missing":
        publish_blocking(path, PROJECT, b"cached")
        path.write_bytes(b"" if damage == "empty" else path.read_bytes()[:-3] + b"abc")

    async def scenario() -> None:
        await provisioner(tmp_path).ensure_template(None, NAME, keep_stale=False, project=PROJECT)

    run(scenario)
    assert fakes.builds == 1
    assert read_verified(path) == b"built 1"
    assert fakes.boots == []


@pytest.mark.usefixtures("fakes")
def test_a_new_build_removes_stale_snapshots_unless_kept(tmp_path: Path) -> None:
    stale = tmp_path / f"{PROJECT}_stale.snapshot"
    stale.write_bytes(b"old")

    async def scenario(*, keep: bool) -> None:
        await provisioner(tmp_path).ensure_template(None, NAME, keep_stale=keep, project=PROJECT)

    run(lambda: scenario(keep=True))
    assert stale.exists()
    snapshot_path(tmp_path, PROJECT, NAME).unlink()
    run(lambda: scenario(keep=False))
    assert not stale.exists()


def test_an_engine_that_rejects_the_snapshot_at_the_first_boot_gets_one_rebuild(tmp_path: Path, fakes: Fakes) -> None:
    publish_blocking(snapshot_path(tmp_path, PROJECT, NAME), PROJECT, b"cached")
    fakes.script = [SnapshotRejectedError("refused")]

    async def scenario() -> None:
        target = provisioner(tmp_path)
        await target.ensure_template(None, NAME, keep_stale=False, project=PROJECT)
        await target.clone(NAME, PROJECT)

    run(scenario)
    assert fakes.builds == 1
    assert fakes.boots == [b"cached", b"built 1"]
    assert read_verified(snapshot_path(tmp_path, PROJECT, NAME)) == b"built 1"


def test_a_second_rejection_is_an_error_and_a_later_one_never_rebuilds(tmp_path: Path, fakes: Fakes) -> None:
    fakes.script = [SnapshotRejectedError("one"), SnapshotRejectedError("two")]

    async def failing() -> None:
        target = provisioner(tmp_path)
        await target.ensure_template(None, NAME, keep_stale=False, project=PROJECT)
        await target.clone(NAME, PROJECT)

    with pytest.raises(SnapshotRejectedError, match="two"):
        run(failing)
    fakes.builds = 0
    fakes.boots.clear()
    fakes.script = [None, SnapshotRejectedError("late")]

    async def late() -> None:
        target = provisioner(tmp_path)
        await target.ensure_template(None, NAME, keep_stale=False, project=PROJECT)
        await target.clone(NAME, PROJECT)
        await target.clone(NAME, PROJECT)

    with pytest.raises(SnapshotRejectedError, match="late"):
        run(late)
    assert fakes.builds == 0


def test_any_other_start_failure_propagates_and_keeps_the_snapshot(tmp_path: Path, fakes: Fakes) -> None:
    path = snapshot_path(tmp_path, PROJECT, NAME)
    publish_blocking(path, PROJECT, b"cached")
    before = path.read_bytes()
    fakes.script = [InterfaceError("out of memory")]

    async def scenario() -> None:
        target = provisioner(tmp_path)
        await target.ensure_template(None, NAME, keep_stale=False, project=PROJECT)
        await target.clone(NAME, PROJECT)

    with pytest.raises(InterfaceError, match="out of memory"):
        run(scenario)
    assert fakes.builds == 0
    assert path.read_bytes() == before


def test_teardown_abandons_the_instance_and_never_closes_it(tmp_path: Path, fakes: Fakes) -> None:
    async def scenario() -> None:
        target = provisioner(tmp_path)
        hold = await target.ensure_template(None, NAME, keep_stale=False, project=PROJECT)
        first = await target.clone(NAME, PROJECT)
        await target.clone(NAME, PROJECT)
        await target.drop(first)
        assert fakes.databases[0].abandoned == 1
        # the second is a leftover that the hold abandons
        await hold.close()
        assert fakes.databases[1].abandoned == 1
        assert fakes.databases[0].abandoned == 1

    run(scenario)


@pytest.mark.usefixtures("fakes")
def test_without_a_cache_the_snapshot_directory_is_removed_with_the_hold(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr("tempfile.tempdir", None)

    async def scenario() -> None:
        target = EmbeddedProvisioner("embedded://", cache=None)
        hold = await target.ensure_template(None, NAME, keep_stale=False, project=PROJECT)
        assert entries(tmp_path) == 1
        await hold.close()

    run(scenario)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.usefixtures("fakes")
def test_the_url_query_is_part_of_the_identity_and_a_plain_url_adds_nothing(tmp_path: Path) -> None:
    plain = provisioner(tmp_path).identity
    assert plain == ("engine:test",)
    optioned = provisioner(tmp_path, "embedded://?options=-c%20search_path%3Dapp").identity
    assert optioned[:-1] == plain
    assert optioned[-1].startswith("query:")
    assert "search_path" in optioned[-1]
    assert optioned != provisioner(tmp_path, "embedded://?options=-c%20search_path%3Dother").identity


@pytest.mark.usefixtures("fakes")
def test_a_directory_url_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="fresh database from a snapshot"):
        provisioner(tmp_path, "embedded:///var/tmp/db")


def test_the_digest_is_a_sha256_of_the_snapshot_alone(tmp_path: Path) -> None:
    path = snapshot_path(tmp_path, PROJECT, NAME)
    publish_blocking(path, PROJECT, b"abc")
    assert path.read_bytes() == b"abc" + hashlib.sha256(b"abc").digest()
