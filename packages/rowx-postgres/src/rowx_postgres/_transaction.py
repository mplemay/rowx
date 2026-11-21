from typing import TYPE_CHECKING, Self

from rowx_core._transaction import BaseTransaction, TrackedStream  # ruff: ignore[import-private-name]
from rowx_core.errors import InterfaceError

from rowx_postgres._core import Connection as ConnectionImpl, RowShape

if TYPE_CHECKING:
    from rowx_core._connection import BaseConnection

__all__ = ["TrackedStream", "Transaction"]


class Transaction(BaseTransaction[ConnectionImpl, RowShape]):
    def __init__(
        self,
        conn: BaseConnection[ConnectionImpl, RowShape],
        /,
        *,
        isolation: str | None = None,
        readonly: bool = False,
        deferrable: bool = False,
        parent: Self | None = None,
    ) -> None:
        super().__init__(
            conn,
            lambda impl: impl._begin(isolation, readonly, deferrable),  # ruff: ignore[private-member-access]
            parent=parent,
        )

    def transaction(
        self,
        *,
        isolation: str | None = None,
        readonly: bool = False,
        deferrable: bool = False,
    ) -> Transaction:
        if isolation is not None or readonly or deferrable:
            msg = "a savepoint takes no isolation, readonly or deferrable arguments"
            raise InterfaceError(msg)
        self._check_active()
        return Transaction(self._conn, parent=self)
