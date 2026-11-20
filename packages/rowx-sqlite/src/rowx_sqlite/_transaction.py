from typing import TYPE_CHECKING, Self

from rowx_core._transaction import BaseTransaction, TrackedStream  # ruff: ignore[import-private-name]
from rowx_core.errors import InterfaceError

from rowx_sqlite._core import Connection as ConnectionImpl, RowShape

if TYPE_CHECKING:
    from rowx_core._connection import BaseConnection

    from rowx_sqlite._options import TransactionMode

__all__ = ["TrackedStream", "Transaction"]


# `read_only` runs a deferred transaction with `PRAGMA query_only` on for its length; without a mode the transaction is
# immediate, so a write never has to upgrade a read lock
class Transaction(BaseTransaction[ConnectionImpl, RowShape]):
    def __init__(
        self,
        conn: BaseConnection[ConnectionImpl, RowShape],
        /,
        *,
        mode: TransactionMode | None = None,
        read_only: bool = False,
        parent: Self | None = None,
    ) -> None:
        super().__init__(
            conn,
            lambda impl: impl._begin(mode, read_only),  # ruff: ignore[private-member-access]
            parent=parent,
        )

    def transaction(self, *, mode: TransactionMode | None = None, read_only: bool = False) -> Transaction:
        if mode is not None or read_only:
            msg = "a savepoint takes no mode or read_only argument"
            raise InterfaceError(msg)
        self._check_active()
        return Transaction(self._conn, parent=self)
