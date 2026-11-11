# Errors

When each exception is raised is in [Errors](../../../../docs/guide/errors.md). The class tree and attributes are
[Errors](../../../../docs/reference/api.md#errors).

Classes are defined in `rowx_core.errors`. `rowx` and `rowx.errors` re-export those same objects.
`rowx.errors.RowNotFound` is `rowx_core.errors.RowNotFound`, and `from rowx import RowNotFound` is that object.
There is no Postgres-specific exception tree.

```python
from rowx_core.errors import InterfaceError, RowNotFound
```

Constructing a query raises `TypeError` for a bad template: a non-template, a NUL in the static text, a conversion,
an unsupported format spec, and an `:ident` value that is not a non-empty `str` (or a tuple of them) without a NUL
or a control character. When the method runs, the compiler raises `TypeError` for an `:ident` outside plain code, a
name longer than 63 bytes, and more than 65535 parameters.

`one` raises `RowNotFound` when the result has no row. Another operation on a connection that is streaming raises
`ConnectionBusyError`. A transaction used after it has ended, a pooled connection used after its lease, and a
session-encoding change raise `InterfaceError`. A value the driver cannot decode raises `DecodeError`. A bad
migration file raises `MigrationSourceError`. Import those names from `rowx_core.errors`, `rowx.errors`, or `rowx`.
Do not invent a driver-specific subclass.
