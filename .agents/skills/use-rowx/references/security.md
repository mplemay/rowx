# Security

What the application must allowlist, and what the pool reset clears, is in
[Security](../../../../docs/guide/security.md). The contract is
[Security and safety](../../../../docs/reference/security.md). The reset order is
[Hooks and the reset](../../../../docs/guide/executors.md#hooks-and-the-reset).

## Identifiers

A table or column name cannot be a parameter. `{name:ident}` is only for `query_unchecked`, and only after the
application allowlists the name. Quoting is not an allowlist. Use a `StrEnum` for raw input, or a `Literal` with
`assert_never` when the value is already typed. Pairs are in [Identifiers](../rules/identifiers.md).

## Pool reset

`reset_query=None` is unsafe for a pool that serves more than one tenant or role. A custom `reset_query` replaces
`RESET_QUERY` entirely, so keep every part you rely on. Pairs are in [Pool reset](../rules/reset.md).

## Values

Every `{name}` is a bound parameter. `Template(...)` built from a non-literal string is raw SQL. The checker rule
`template-construction` rejects that form under the include roots. It is best-effort; the guide lists what it
cannot see.
