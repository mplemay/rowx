# Conversation: SQLx vs SQLTy — SQL validation, Python wrappers, and checkers

Saved from a Cursor chat exploring how [SQLx](https://github.com/launchbadge/sqlx) and
[SQLTy](https://github.com/jirikuncar/sqlty) parse/validate SQL, and whether a Python library / static checker could
work like SQLx.

---

## 1. How SQLx and SQLTy parse / validate SQL

### Big picture

| | **SQLx** (Rust) | **sqlty** (Python) |
|--|-----------------|---------------------|
| How SQL is “parsed” | **Not client-side** — server prepare/describe | **sqlparser-rs** AST + custom analyzer |
| Schema source of truth | Live DB (or cached describe JSON) | Repo `CREATE TABLE` / migrations / casts |
| Aligns with model via | Proc macros → generated Rust types + rustc | `SQL[T]` + rewrite/check + mypy/pyright |
| Offline CI | `.sqlx` query cache from `cargo sqlx prepare` | Native — no DB required |

They solve the same problem from opposite directions: **SQLx asks the database**;
**sqlty parses and typechecks offline**.

### SQLx: database-backed describe → Rust types

- **No SQL parser crate** (no `sqlparser`, `pg_query`, etc.).
- Syntax/schema validity come from the engine:
  - **Postgres**: extended query `Parse` + `Describe` (statement); nullability via `pg_attribute` + `EXPLAIN`
  - **MySQL**: `COM_STMT_PREPARE` (param **count**, weak/no param types)
  - **SQLite**: `sqlite3_prepare_v3` + decltype / `EXPLAIN`
- Macros (`query!`, `query_as!`, …) consume a `Describe` (columns, parameters, nullability).
- Offline: `cargo sqlx prepare` writes `query-<sha256>.json` under `.sqlx`; builds without `DATABASE_URL` load that
  cache.
- FAQ rationale: reimplementing dialect parse + typecheck + migration simulation for every backend would mean
  reimplementing a large chunk of each server’s frontend.

### sqlty: sqlparser-rs + offline analyzer → `SQL[T]`

- Does **not** prepare against Postgres for checking (live prepare exists only in fidelity tests / scripts).
- Engine is Rust (`sqlty-sql`), exposed as `sqlty._core` via PyO3.
- **Libraries / techniques:**
  - `sqlparser` 0.63 (visitor) → AST
  - Preprocess / respell (placeholders → `NULL` for typing; dialect quirks)
  - Catalog from DDL (tables, views, enums, domains, PG ALTER/functions)
  - Custom analyzer (scopes, resolve, expr typing, nullability)
  - Inference rules: `*.inference.toml` → `build.rs` → static `match`
  - `SQL[T]` / `SQLTemplate[T]` for type checkers; structural checks against TypedDict / NamedTuple / dataclass /
    Pydantic
- Queries marked with `--sql\n`; `sqlty rewrite` / `check` keep annotations in sync.

### What each validates

**SQLx macros:** engine accepts query · result columns/types · best-effort nullability · param count · (Postgres) param
SQL→Rust types.

**sqlty:** inferred row types · wrapper drift (`stale-type` / `narrow-type`) · model mismatches · placeholder
arity/types · unknown table/column (with schema). Not “will this exact server accept this syntax?” — unparsed queries
stay untyped / warned.

---

## 2. Which approach is generally better?

Neither is generally better — they optimize for different failure modes.

- Prefer **SQLx** when you care most that the query is **accepted by the real engine** and types match **that** schema.
- Prefer **sqlty** when you care most about **no DB in CI**, **inline string literals**, and catching
  **annotation/model drift** against a schema dump.

If forced to pick a default for production systems: **DB-backed describe (SQLx) is the stronger guarantee**; offline
inference (sqlty) is the stronger **workflow**. Mature setups often want both ideas (exactness + offline CI).

| Priority | Better fit |
|----------|------------|
| “Will this query run on our DB?” | SQLx |
| “Will CI stay green without a database?” | sqlty |
| “Did our row type / Pydantic model drift?” | sqlty (more explicit) |
| “Exact column OIDs / server nullability” | SQLx |
| Python string-literal SQL culture | sqlty |
| Rust compile-time checked queries | SQLx |

---

## 3. Should SQLx use datafusion-sqlparser-rs?

**No — it doesn’t need it** for its core job.

A parser only gives an AST. To match what macros get today you’d still need schema-aware type analysis, migration
simulation, and correctness vs real engine quirks — which the FAQ explicitly rejects.

sqlparser *could* help peripheral DX (diagnostics, param-nullability analysis, linting) but would not replace
prepare/describe. sqlty needs a parser because it **refuses** to ask the DB; SQLx’s product promise is the opposite.

---

## 4. Python library “on top of SQLx”?

### Desired APIs discussed

```python
# Variant A
val = await query_as(t"Select {num}").fetch_one(conn)

# Variant B
val = await query[int](t"Select {num}").fetch_one(conn)
```

### Verdict

- **Partially yes for runtime; no for SQLx’s real magic.**
- You can wrap SQLx’s *runtime* API via PyO3 (t-string → parameterized SQL → execute).
- You **cannot** get `query!` / `query_as!` compile-time checking by importing sqlx into Python — those are Rust proc
  macros that run in `rustc`.
- `query[int](...)` is a fine Python API: type checkers trust `[int]`; that does **not** mean the SQL was validated like
  SQLx macros.
- Building on SQLx is usually the wrong substrate for Python (Tokio + packaging + foreign connections) when
  psycopg3/asyncpg already exist.
- **sqlty** already targets t-strings with `SQLTemplate[T]` — typed raw SQL without SQLx underneath.

---

## 5. Could you set up a static checker like sqlty?

**Yes.** For `query[T](t"...")` a checker would:

1. Find call sites via Python AST
2. Rebuild SQL from the t-string (static parts + placeholders)
3. Infer types (offline analyzer **or** DB describe)
4. Compare to the subscript (`int`, `User`, …)
5. Optionally rewrite annotations

sqlty already does most of this for `--sql` t-strings → `SQLTemplate[T]`. Fastest path: extend/wrap sqlty rather than
rebuild under a PyO3 sqlx bridge.

| Style | Like | Pros | Cons |
|-------|------|------|------|
| Offline schema + AST | sqlty | No DB in CI; rewrite/`breaking` | Parser/rule gaps |
| Prepare/describe at check time | SQLx / SafeQL | Exact server types | Needs DB or cache |

---

## 6. Could the static checker work like SQLx (validate each query)?

**Yes.** A Python tool can validate each query by prepare/describe:

1. Extract SQL + placeholders from `t"..."`
2. `PREPARE` / describe on the DB
3. Server reject → checker error
4. Map described types → Python; compare to `[T]`
5. Optional: write `query-<hash>.json` for offline CI (SQLx-style cache)

Python won’t run this inside `import` the way rustc runs macros; it runs as a **CLI / pre-commit / CI / LSP** tool that
still validates **each** query the same way. You do not need the full sqlx crate — psycopg prepare/describe (or a small
helper) is enough.

---

## 7. How would this handle migrations?

A SQLx-style checker doesn’t “understand” migration files. It only sees
**whatever schema is on the database you prepare against**. Migrations are handled by **workflow**:

```text
migrations applied to DB  →  checker prepares each query against that DB
                          →  (optional) refresh offline cache
```

### Typical pipelines

1. **Ephemeral CI DB** — migrate to head, live-check all queries
2. **Migrate + prepare + commit cache** — fast offline CI; must refresh after schema/query changes
3. **Expand/contract migrations** — so old and new queries both prepare during rollout

### Rolling deploys

Prepare-against-head only answers: “does new code work on new schema?” For migrate-first / deploy-first you need an
explicit matrix (old queries × new DB, new queries × old DB) — analogous to sqlty’s `breaking --against`, but with two
DB migrate targets instead of two schema files.

| | SQLx-like (prepare) | sqlty |
|--|---------------------|--------|
| Migration input | Applied state of a DB | Schema dump / migration output as files |
| After migrate | Point checker at DB (or refresh cache) | Update schema file, `check` / `rewrite` |
| Cross-version | Two DBs / two migrate stops | `breaking --against` on two schema files |

---

## Key file pointers (from exploration)

### SQLx

- `sqlx-core/src/describe.rs` — `Describe` for macros
- `sqlx-macros-core/src/query/` — live vs offline expand
- `sqlx-postgres/.../executor.rs` — Parse + Describe
- `sqlx-cli/src/prepare.rs` — `.sqlx` cache
- `FAQ.md` — “Why can’t SQLx just parse the SQL itself?”

### sqlty

- `rust/crates/sqlty-sql/` — parse, catalog, analyzer, rules
- `rust/crates/sqlty-sql/rules/` — inference TOML → compiled match
- `src/sqlty/` — `SQL[T]`, `SQLTemplate[T]`, check/rewrite
- `docs/motivation.md` — comparison table including sqlx
- `docs/guide/breaking-changes.md` — cross-version schema checks

---

## Bottom lines

1. **SQLx** = ask the DB (or cache of that ask); no client SQL parser needed.
2. **sqlty** = parse offline with sqlparser-rs + custom typing; no DB at check time.
3. A Python `query[T](t"...")` API is easy; **SQLx compile-time macros do not port**.
4. A **SQLx-like Python checker** (prepare each query) is absolutely doable as a tool + optional cache.
5. **Migrations** for that checker = apply them to the check DB (and refresh cache); cross-version safety is a separate,
   deliberate step.
