# rowx design

rowx is typesafe raw SQL for Python 3.14+, with no ORM. You write queries as PEP 750 t-strings with an explicit row
type, `query[tuple[int, str]](t"...")`. A Rust driver built on tokio-postgres runs them. A static checker,
`rowx check`, validates every query against a **live** Postgres 18 database, the same way SQLx's macros do, by
preparing and describing it.

This directory is the **contract** for v1. Where it says **MUST**, the behaviour is required and has a test.
"Decision" callouts record a choice and the reason for it.

| Document | Covers |
|---|---|
| [Overview](overview.md) | Goals, non-goals and prior art |
| [API](api.md) | Public surface, row-type grammar, t-string compilation, overrides and errors |
| [Architecture](architecture.md) | Python/Rust layering, packaging and toolchain |
| [Type mapping](type-mapping.md) | The single type table shared by decoder, encoder and checker |
| [Driver](driver.md) | DSN and TLS, execution path, codecs, statement cache, state machine, cancellation |
| [Nullability](nullability.md) | Describe, the nullability algorithm, the EXPLAIN plan walk, known limits |
| [Checker](checker.md) | `rowx check`: CLI, config, discovery, source map, row-type resolution, rules, output |
| [Testing](testing.md) | Test strategy and CI |
| [Dependencies](dependencies.md) | Which crates and tools were evaluated, and the verdicts |
| [Security](security.md) | Parameter binding, checker safety and TLS defaults |
| [Roadmap](roadmap.md) | Deferred work, by expected value |
| [Decision log](decision-log.md) | Design interview and review decisions |

User-facing documentation is in [`../guide.md`](../guide.md).

## Change process

- A feature PR **must not** change the design silently.
- For a spec gap, describe the question and decision in the PR, then update the design in a follow-up docs PR.
- Changes to public behaviour need a design PR first.
