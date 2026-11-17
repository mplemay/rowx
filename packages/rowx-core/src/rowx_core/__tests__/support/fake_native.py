from typing import TYPE_CHECKING

from rowx_core import Compiled, ShapeSpec, TemplatePlan

if TYPE_CHECKING:
    from collections.abc import Awaitable, Sequence


class FakeStream:
    rows_affected = None

    def __init__(self, failure: BaseException | None = None) -> None:
        self.failure = failure
        self.closed = False

    async def _next(self) -> list[object]:
        if self.failure is not None:
            raise self.failure
        return []

    def close(self) -> None:
        self.closed = True


class FakeNative:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.failure: BaseException | None = None
        self.stream = FakeStream()
        self.closed = False
        self.log: list[str] = []
        self.next_savepoint = 0
        self.end_failure: BaseException | None = None

    def _record(self, name: str, *args: object) -> None:
        self.calls.append((name, args))
        if self.failure is not None:
            raise self.failure

    async def _fetch(  # ruff: ignore[too-many-arguments, too-many-positional-arguments]
        self,
        sql: str,
        params: list[object],
        mask: Sequence[int] | None,  # ruff: ignore[unused-method-argument]
        scalar: bool,  # ruff: ignore[unused-method-argument, boolean-type-hint-positional-argument]
        limit_one: bool,  # ruff: ignore[unused-method-argument, boolean-type-hint-positional-argument]
        arity: int | None,  # ruff: ignore[unused-method-argument]
        shape: object,
    ) -> list[object]:
        self._record("fetch", sql, params, shape)
        return [1]

    def _stream(  # ruff: ignore[too-many-arguments, too-many-positional-arguments]
        self,
        sql: str,
        params: list[object],  # ruff: ignore[unused-method-argument]
        mask: Sequence[int] | None,  # ruff: ignore[unused-method-argument]
        scalar: bool,  # ruff: ignore[unused-method-argument, boolean-type-hint-positional-argument]
        arity: int | None,  # ruff: ignore[unused-method-argument]
        batch_size: int,  # ruff: ignore[unused-method-argument]
        shape: object,
    ) -> FakeStream:
        self._record("stream", sql, shape)
        return self.stream

    async def _execute(self, sql: str, params: list[object]) -> int:
        self._record("execute", sql, params)
        return 3

    async def ping(self) -> None:
        self._record("ping")

    async def close(self) -> None:
        self.closed = True

    def detach(self) -> None:
        self._record("detach")

    async def _savepoint(self) -> int:
        self.next_savepoint += 1
        self.log.append(f"savepoint {self.next_savepoint}")
        return self.next_savepoint

    async def _release_savepoint(self, n: int) -> None:
        self.log.append(f"release {n}")

    async def _rollback_to_savepoint(self, n: int) -> None:
        self.log.append(f"rollback to {n}")

    def _end_transaction(
        self,
        commit: bool,  # ruff: ignore[boolean-type-hint-positional-argument]
    ) -> Awaitable[None]:
        # a busy connection raises when called, before anything is awaited
        if self.end_failure is not None:
            raise self.end_failure

        # the protocol wants an awaitable, and the fake has nothing to wait for
        async def end() -> None:  # ruff: ignore[unused-async]
            self.log.append("commit" if commit else "rollback")

        return end()


class FakeCompiler:
    def compile(self, plan: TemplatePlan, /) -> tuple[Compiled, list[object]]:
        raise NotImplementedError

    def compile_parts(self, strings: Sequence[str], /) -> Compiled:
        raise NotImplementedError


COMPILER = FakeCompiler()


def native_shape(spec: object) -> object:
    return ("native", spec) if isinstance(spec, ShapeSpec) else spec
