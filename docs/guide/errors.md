# Errors

Classes are defined in `rowx_core.errors`. `rowx` and `rowx.errors` re-export those same objects.
`rowx.errors.RowNotFound` is `rowx_core.errors.RowNotFound`, and `from rowx import RowNotFound` is that object.
The native error registry uses those classes. There is no Postgres-specific exception tree. The classes and their
attributes are [Errors](../reference/api.md#errors).

Constructing a query raises `TypeError` for a bad template: a value that is not a template, a NUL in the static
text, a conversion, an unsupported format spec, and an `:ident` value that is not a non-empty `str` (or a tuple of
them) without a NUL or a control character.

When the method runs, that executor's compiler emits `$n` and the adjacency space, quotes the captured identifier,
and raises `TypeError` for an `:ident` outside plain code, a name longer than 63 bytes, and more than 65535
parameters.

`one` raises `RowNotFound` when the result has no row ([Get started](get-started.md#quickstart)). Another
operation on a connection that is streaming raises `ConnectionBusyError` ([Streaming](streaming.md)). A
transaction used after it has ended, a pooled connection used after its lease, and a session-encoding change raise
`InterfaceError` ([Transactions](executors.md#transactions),
[Deployment caveats](executors.md#deployment-caveats), [Session state](security.md#session-state)). A value the
driver cannot decode raises `DecodeError` ([Known limits](../guide.md#known-limits)).
