# Deferred work (by expected value)

1. **Named row types** (dataclass, NamedTuple, TypedDict, Pydantic, Protocol), matched by
   column name.
2. A **transaction helper** (`async with conn.transaction()`), then opt-in **pipelining**.
3. **Pool**, using deadpool-postgres.
4. **Offline cache** (`rowx prepare`, `.rowx/query-<sha256>.json`, `--check` drift).
5. `fetch_optional`, and streaming `fetch` as an async iterator.
6. `--fix`, warnings and ignore comments.
7. **Type overrides** (`"col: T"`) and custom codecs (domains, composites, ranges, `inet`,
   `money`).
8. Parser-based nullability narrowing ([Dependency evaluation](dependencies.md#dependency-evaluation)), and a
   cross-version `breaking --against` check.
9. LISTEN/NOTIFY, COPY, client-cert TLS, `PG*` env vars and `.pgpass`, and a free-threaded
   build.
10. Other backends.
