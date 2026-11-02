# Deferred work (by expected value)

1. **The rest of named row types.** Dataclass, `NamedTuple`, `TypedDict` and pydantic rows are supported
   ([Named row types](api.md#named-row-types)). Still deferred:
   - attrs, msgspec and `Protocol`;
   - nested and JSON-shaped fields, and composite and record columns (type parity T1 stays deferred), with sqlx's
     `flatten`;
   - a pydantic type table and lax-aware checking (`EmailStr` and friends);
   - named types as query **parameters**, and row classes from installed packages.
2. Opt-in **pipelining**.
3. **Offline cache** (`rowx prepare`, `.rowx/query-<sha256>.json`, `--check` drift).
4. **Multi-statement execution** over the simple protocol (sqlx's `fetch_many` results),
   and the `CommandComplete` tag on `QueryResult`.
5. `--fix`, warnings and ignore comments.
6. **Type overrides** (`"col: T"`) and user-supplied custom codecs (domains and composites). Ranges, `inet` and
   `money` are no longer here: they are built-in codecs ([Type mapping](type-mapping.md)).
7. Parser-based nullability narrowing ([Dependency evaluation](dependencies.md#dependency-evaluation)), and a
   cross-version `breaking --against` check. [Migrations](migrations.md) now give it a way to build the old and new
   schemas (for example with `--scratch`).
8. LISTEN/NOTIFY, COPY, client-cert TLS, `PG*` env vars and `.pgpass`, and a free-threaded
   build.
9. Other backends. The layout exists (`rowx-core`, a backend's `-<name>` and `-<name>-py` crates, and a
   `rowx.<name>` namespace), so a new backend only adds those.
10. **Package-resource migrations**: loading migrations through `importlib.resources` instead of a directory path.
11. **Per-test SQL fixtures** for the `rowx.testing` plugin (sqlx `#[sqlx::test(fixtures(...))]`).
