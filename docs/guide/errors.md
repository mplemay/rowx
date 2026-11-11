# Errors

Classes are defined in `rowx_core.errors`. `rowx` and `rowx.errors` re-export those same objects.
`rowx.errors.RowNotFound` is `rowx_core.errors.RowNotFound`, and `from rowx import RowNotFound` is that object.
The native error registry uses those classes. There is no Postgres-specific exception tree. The classes and their
attributes are [Errors](../reference/api.md#errors).

Constructing a query raises `TypeError` for a bad template: a value that is not a template, a NUL in the static
text, a conversion, or an unsupported format spec.

PostgreSQL quoting, lexical `:ident` placement, and the 65535 parameter cap are raised when the method runs, by
that executor's compiler.

`one` raises `RowNotFound` when the result has no row ([Get started](get-started.md#quickstart)). Another
operation on a connection that is streaming raises `ConnectionBusyError` ([Streaming](streaming.md)). A
transaction used after it has ended, a pooled connection used after its lease, and a session-encoding change raise
`InterfaceError` ([Transactions](executors.md#transactions),
[Deployment caveats](executors.md#deployment-caveats), [Session state](security.md#session-state)). A value the
driver cannot decode raises `DecodeError` ([Known limits](../guide.md#known-limits)).
