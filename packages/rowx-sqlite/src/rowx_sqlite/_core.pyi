from collections.abc import Sequence
from typing import Any, Literal

from rowx_sqlite._options import ConnectOptions, TransactionMode

type RowFormName = Literal["positional", "keyword", "dict", "validator"]
type MatchingName = Literal["by_name", "positional"]
type IssueKind = Literal[
    "missing-column",
    "unexpected-column",
    "duplicate-column",
    "ambiguous-column",
    "column-name-mismatch",
    "arity-mismatch",
]
type TypeKey = Literal[
    "integer",
    "real",
    "text",
    "blob",
    "boolean",
    "datetime",
    "datetimetz",
    "date",
    "time",
    "decimal",
    "uuid",
    "json",
    "any",
]

def sqlite_version() -> str: ...

class TypeInfo:
    @property
    def key(self) -> TypeKey: ...
    @property
    def py_result(self) -> str: ...
    @property
    def tz(self) -> Literal["none", "aware", "naive"]: ...
    @property
    def accepted_params(self) -> list[str]: ...
    @property
    def codec(self) -> str: ...

def type_table() -> list[TypeInfo]: ...

class RowShape:
    def __init__(
        self,
        ctor: object,
        form: RowFormName,
        matching: MatchingName,
        type_name: str,
        fields: Sequence[tuple[str, Sequence[str] | None, bool, int]],
        /,
        *,
        validate: bool = True,
    ) -> None: ...
    @property
    def ctor(self) -> object: ...
    @property
    def form(self) -> RowFormName: ...
    @property
    def matching(self) -> MatchingName: ...
    @property
    def type_name(self) -> str: ...

class ShapeIssue:
    @property
    def kind(self) -> IssueKind: ...
    @property
    def columns(self) -> list[int]: ...
    @property
    def fields(self) -> list[int]: ...
    @property
    def name(self) -> str | None: ...
    @property
    def min(self) -> int | None: ...
    @property
    def max(self) -> int | None: ...
    @property
    def actual(self) -> int | None: ...

def map_columns(
    columns: Sequence[str],
    fields: Sequence[tuple[Sequence[str] | None, bool]],
    positional: bool,
) -> tuple[list[int | None], list[ShapeIssue]]: ...

class RowStream:
    async def _next(self) -> list[object]: ...
    def close(self) -> None: ...
    @property
    def rows_affected(self) -> int | None: ...

class Connection:
    async def _fetch(
        self,
        sql: str,
        params: list[object],
        mask: Sequence[int] | None,
        scalar: bool,
        limit_one: bool,
        arity: int | None,
        shape: RowShape | None,
    ) -> list[object]: ...
    def _stream(
        self,
        sql: str,
        params: list[object],
        mask: Sequence[int] | None,
        scalar: bool,
        arity: int | None,
        batch_size: int,
        shape: RowShape | None,
    ) -> RowStream: ...
    async def _execute(self, sql: str, params: list[object]) -> int: ...
    async def ping(self) -> None: ...
    async def close(self) -> None: ...
    def detach(self) -> None: ...
    async def _begin(self, mode: TransactionMode | None, read_only: bool) -> int: ...
    async def _savepoint(self) -> int: ...
    async def _release_savepoint(self, n: int) -> int: ...
    async def _rollback_to_savepoint(self, n: int) -> int: ...
    async def _end_transaction(self, commit: bool) -> None: ...
    @property
    def closed(self) -> bool: ...
    @property
    def statement_cache_size(self) -> int: ...
    @property
    def variable_limit(self) -> int: ...

async def connect(options: ConnectOptions, /) -> Connection: ...
def parse_dsn(dsn: str, /) -> dict[str, Any]: ...
