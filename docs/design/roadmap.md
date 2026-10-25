# Deferred work (by expected value)

1. **Named row types** (dataclass, NamedTuple, TypedDict, Pydantic, Protocol), matched by
   column name.
2. Opt-in **pipelining**.
3. **Offline cache** (`rowx prepare`, `.rowx/query-<sha256>.json`, `--check` drift).
4. **Multi-statement execution** over the simple protocol (sqlx's `fetch_many` results),
   and the `CommandComplete` tag on `QueryResult`.
5. `--fix`, warnings and ignore comments.
6. **Type overrides** (`"col: T"`) and custom codecs (domains, composites, ranges, `inet`,
   `money`).
7. Parser-based nullability narrowing ([Dependency evaluation](dependencies.md#dependency-evaluation)), and a
   cross-version `breaking --against` check.
8. LISTEN/NOTIFY, COPY, client-cert TLS, `PG*` env vars and `.pgpass`, and a free-threaded
   build.
9. Other backends. The layout exists (`rowx-core`, a backend's `-<name>` and `-<name>-py` crates, and a
   `rowx.<name>` namespace), so a new backend only adds those.
