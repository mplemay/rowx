# Security

## Identifiers

A table or column name cannot be a parameter. `{name:ident}` is only for `query_unchecked`, and only after the
application allowlists the name. Quoting is not an allowlist. Use a `StrEnum` for raw input, or a `Literal` with
`assert_never` when the value is already typed. Pairs are in [Identifiers](../rules/identifiers.md).

## Pool reset

`reset_query=None` is unsafe for a pool that serves more than one tenant or role. A custom `reset_query` replaces
`RESET_QUERY` entirely, so keep every part you rely on. The default is `RESET_QUERY`:

```text
RESET SESSION AUTHORIZATION; RESET ALL; SELECT pg_catalog.pg_advisory_unlock_all(); CLOSE ALL; UNLISTEN *; DISCARD TEMP;
```

Pairs are in [Pool reset](../rules/reset.md).

## Values

Every `{name}` is a bound parameter. `Template(...)` built from a non-literal string is raw SQL. The checker rule
`template-construction` rejects that form under the include roots. It reads source only, so `getattr`, `eval`,
dynamic imports, generated code, and files outside the include roots can still build a template.
