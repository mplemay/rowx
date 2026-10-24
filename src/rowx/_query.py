from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from types import TracebackType

    from rowx._core import Connection as ConnectionImpl


class Connection:
    def __init__(self, impl: ConnectionImpl, /) -> None:
        self._impl = impl

    async def close(self) -> None:
        await self._impl.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    @property
    def closed(self) -> bool:
        return self._impl.closed
