import asyncio
import os
import random
import secrets
from collections import defaultdict
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Final, Literal

import psycopg

from rowx._core.postgres import connect_checker, describe

if TYPE_CHECKING:
    from collections.abc import Callable

DEFAULT_SEED: Final[int] = 109
DEFAULT_COUNT: Final[int] = 300
SEED_ENV: Final[str] = "ROWX_FUZZ_SEED"
COUNT_ENV: Final[str] = "ROWX_FUZZ_COUNT"

type ColType = Literal["int", "text", "num"]
type Wrap = Literal[
    "none",
    "subquery",
    "cte",
    "cte_mat",
    "cte_inline",
    "cte_twice",
    "recursive",
    "union",
    "intersect",
    "except",
    "union_all",
    "nested_left",
    "nested_right",
]
type Other = Literal["same", "empty", "nulls"]

SQL_TYPES: Final[dict[ColType, str]] = {"int": "integer", "text": "text", "num": "numeric"}
TABLES: Final[dict[str, tuple[tuple[str, ColType], ...]]] = {
    "users": (("id", "int"), ("name", "text"), ("email", "text")),
    "posts": (("id", "int"), ("user_id", "int"), ("title", "text")),
    "orders": (("id", "int"), ("user_id", "int"), ("amount", "num"), ("note", "text")),
    "v_users": (("id", "int"), ("name", "text"), ("email", "text")),
    "v_posts": (("id", "int"), ("user_id", "int"), ("title", "text")),
    "v_orders": (("id", "int"), ("user_id", "int"), ("amount", "num"), ("note", "text")),
}
JOIN_KINDS: Final[tuple[str, ...]] = ("INNER JOIN", "LEFT JOIN", "RIGHT JOIN", "FULL JOIN")
LATERAL_KINDS: Final[tuple[str, ...]] = ("INNER JOIN LATERAL", "LEFT JOIN LATERAL")
WRAPS: Final[tuple[Wrap, ...]] = (
    "none",
    "none",
    "subquery",
    "cte",
    "cte_mat",
    "cte_inline",
    "cte_twice",
    "recursive",
    "union",
    "intersect",
    "except",
    "union_all",
    "nested_left",
    "nested_right",
)
GROUPS: Final[tuple[str, ...]] = ("", "", "rollup", "cube", "sets")
WINDOWS: Final[tuple[str, ...]] = (
    "row_number() OVER ()",
    "count(*) OVER ()",
    "lag({c}) OVER ()",
    "max({c}) OVER (PARTITION BY {c})",
)
RETURNING_POOL: Final[dict[str, tuple[str, ...]]] = {
    "users": ("id", "name", "email", "old.id", "old.name", "old.email", "new.name", "new.email", "merge_action()"),
    "posts": ("id", "user_id", "title", "old.title", "new.title", "old.user_id", "new.id"),
}


@dataclass(slots=True, frozen=True, kw_only=True)
class Col:
    expr: str
    type: ColType
    name: str


@dataclass(slots=True, frozen=True, kw_only=True)
class Join:
    kind: str
    source: str
    alias: str
    on: str


@dataclass(slots=True, frozen=True, kw_only=True)
class Shape:
    base: str
    joins: tuple[Join, ...]
    columns: tuple[Col, ...]
    where: str = ""
    group: str = ""
    window: str = ""
    wrap: Wrap = "none"
    other: Other = "same"


@dataclass(slots=True, frozen=True, kw_only=True)
class Generated:
    sql: str
    shape: Shape | None = None


@dataclass(slots=True, frozen=True, kw_only=True)
class Failure:
    sql: str
    columns: list[int]
    reduced: str


@dataclass(slots=True, kw_only=True)
class Counts:
    nullable: int = 0
    never_null: int = 0
    examples: list[str] = field(default_factory=list)


@dataclass(slots=True, kw_only=True)
class FuzzReport:
    seed: int
    count: int
    statements: int = 0
    failures: list[Failure] = field(default_factory=list)
    precision: dict[str, Counts] = field(default_factory=lambda: defaultdict(Counts))

    def render(self) -> str:
        lines = [
            f"nullability fuzz: seed={self.seed} count={self.count} statements={self.statements}",
            f"soundness failures: {len(self.failures)}",
        ]
        for failure in self.failures:
            lines.extend([f"  columns {failure.columns}: {failure.sql}", f"    reduced: {failure.reduced}"])
        lines.extend([
            "precision (columns describe marks nullable, over statements that returned rows):",
            f"  {'reason':<24}{'nullable':>10}{'never null':>12}",
        ])
        for reason, counts in sorted(self.precision.items()):
            lines.append(f"  {reason:<24}{counts.nullable:>10}{counts.never_null:>12}")
            lines.extend(f"      e.g. {example}" for example in counts.examples)
        return "\n".join(lines)


# `ROWX_FUZZ_SEED=random` draws a fresh seed, so no shell needs to generate one; the report prints it for replay
def fuzz_settings() -> tuple[int, int]:
    raw = os.environ.get(SEED_ENV, str(DEFAULT_SEED))
    seed = secrets.randbelow(2**31) if raw == "random" else int(raw)
    return seed, int(os.environ.get(COUNT_ENV, DEFAULT_COUNT))


def outputs(shape: Shape) -> list[tuple[str, ColType]]:
    found = [(col.name, col.type) for col in shape.columns]
    if shape.group:
        found.append(("n", "num"))
    if shape.window:
        c = shape.columns[0]
        found.append(("w", c.type if shape.window.startswith(("lag", "max")) else "num"))
    return found


def _core(shape: Shape, prefix: str = "") -> str:
    items = [f"{col.expr} AS {col.name}" for col in shape.columns]
    if shape.group:
        items.append("count(*) AS n")
    if shape.window:
        items.append(f"{shape.window.format(c=shape.columns[0].expr)} AS w")
    parts = ["SELECT", ", ".join([prefix, *items] if prefix else items), "FROM", f"{shape.base} t0"]
    parts.extend(f"{join.kind} {join.source} {join.alias} ON {join.on}" for join in shape.joins)
    if shape.where:
        parts.extend(["WHERE", shape.where])
    if shape.group:
        exprs = ", ".join(col.expr for col in shape.columns)
        grouping = {
            "rollup": f"ROLLUP({exprs})",
            "cube": f"CUBE({exprs})",
            "sets": f"GROUPING SETS (({exprs}), ())",
        }[shape.group]
        parts.extend(["GROUP BY", grouping])
    return " ".join(parts)


def render(shape: Shape) -> str:
    names = [name for name, _ in outputs(shape)]
    wrap = shape.wrap
    inner = _core(shape)
    if wrap == "none":
        return inner
    if wrap == "subquery":
        return " ".join(["SELECT", ", ".join(f"x.{n}" for n in names), "FROM", f"({inner}) x"])
    if wrap in {"cte", "cte_mat", "cte_inline", "cte_twice"}:
        hint = {"cte_mat": "MATERIALIZED ", "cte_inline": "NOT MATERIALIZED "}.get(wrap, "")
        head = f"WITH c AS {hint}({inner})"
        if wrap == "cte_twice":
            items = ", ".join([*(f"a.{n}" for n in names), *(f"b.{n}" for n in names)])
            return " ".join([head, "SELECT", items, "FROM c a LEFT JOIN c b ON", f"a.{names[0]} = b.{names[0]}"])
        return " ".join([head, "SELECT", ", ".join(f"c.{n}" for n in names), "FROM c"])
    if wrap == "recursive":
        listed = ", ".join(["lvl_", *names])
        step = ", ".join(["lvl_ + 1", *(f"r.{n}" for n in names)])
        first = _core(shape, "1")
        return " ".join([
            f"WITH RECURSIVE r({listed}) AS ({first} UNION ALL SELECT {step} FROM r WHERE lvl_ < 2)",  # ruff: ignore[hardcoded-sql-expression]
            "SELECT",
            ", ".join(f"r.{n}" for n in names),
            "FROM r",
        ])
    return _render_set_op(shape, inner)


def _render_set_op(shape: Shape, inner: str) -> str:
    wrap = shape.wrap
    op = {
        "union": "UNION",
        "intersect": "INTERSECT",
        "except": "EXCEPT",
        "union_all": "UNION ALL",
        "nested_left": "UNION ALL",
        "nested_right": "EXCEPT",
    }[wrap]
    match shape.other:
        case "same":
            second = _core(replace(shape, where=""))
        case "empty":
            second = _core(replace(shape, where="false"))
        case "nulls":
            second = "SELECT " + ", ".join(f"NULL::{SQL_TYPES[t]}" for _, t in outputs(shape))
    # a third branch nests a different operation on either side of the first
    third = _core(replace(shape, where=""))
    if wrap == "nested_left":
        return f"(({inner}) {op} ({second})) INTERSECT ({third})"
    if wrap == "nested_right":
        return f"({inner}) {op} (({second}) UNION ({third}))"
    return f"({inner}) {op} ({second})"


def _name_for(expr: str, taken: set[str]) -> str:
    base = expr.rsplit(".", maxsplit=1)[-1]
    name, index = base, 1
    while name in taken:
        name = f"{base}_{index}"
        index += 1
    taken.add(name)
    return name


def _ints(table: str) -> list[str]:
    return [c for c, t in TABLES[table] if t == "int"]


def _texts(table: str) -> list[str]:
    return [c for c, t in TABLES[table] if t == "text"]


def _gen_join(rng: random.Random, alias: str, earlier: str, earlier_table: str) -> Join:
    table = rng.choice(list(TABLES))
    if rng.random() < 0.2:
        limit = rng.randint(1, 2)
        source = f"(SELECT * FROM {table} s WHERE s.id >= {earlier}.{rng.choice(_ints(earlier_table))} LIMIT {limit})"  # ruff: ignore[hardcoded-sql-expression]
        return Join(kind=rng.choice(LATERAL_KINDS), source=source, alias=alias, on="true")
    if rng.random() < 0.25 and _texts(table) and _texts(earlier_table):
        on = f"{alias}.{rng.choice(_texts(table))} = {earlier}.{rng.choice(_texts(earlier_table))}"
    elif rng.random() < 0.1:
        on = "true"
    else:
        on = f"{alias}.{rng.choice(_ints(table))} = {earlier}.{rng.choice(_ints(earlier_table))}"
    return Join(kind=rng.choice(JOIN_KINDS), source=table, alias=alias, on=on)


def _gen_where(rng: random.Random, pool: list[tuple[str, ColType]]) -> str:
    if rng.random() < 0.5:
        return ""
    expr, _ = rng.choice(pool)
    return rng.choice([f"{expr} IS NOT NULL", f"{expr} IS NULL", "false", f"{expr} IS NOT NULL"])


def _gen_shape(rng: random.Random) -> Shape:
    base = rng.choice(list(TABLES))
    aliases: dict[str, str] = {"t0": base}
    joins: list[Join] = []
    for index in range(1, rng.randint(1, 4)):
        earlier = rng.choice(list(aliases))
        join = _gen_join(rng, f"t{index}", earlier, aliases[earlier])
        joins.append(join)
        aliases[join.alias] = join.source if join.source in TABLES else _lateral_table(join.source)
    pool = [(f"{a}.{c}", t) for a, table in aliases.items() for c, t in TABLES[table]]
    chosen = rng.sample(pool, k=rng.randint(1, min(4, len(pool))))
    taken: set[str] = set()
    columns = tuple(Col(expr=e, type=t, name=_name_for(e, taken)) for e, t in chosen)
    group = rng.choice(GROUPS)
    return Shape(
        base=base,
        joins=tuple(joins),
        columns=columns,
        where=_gen_where(rng, pool),
        group=group,
        window="" if group or rng.random() < 0.7 else rng.choice(WINDOWS),
        wrap=rng.choice(WRAPS),
        other=rng.choice(["same", "empty", "nulls"]),
    )


# a lateral source is `(SELECT * FROM <table> s ...)`
def _lateral_table(source: str) -> str:
    return source.split(" FROM ", maxsplit=1)[1].split(" ", maxsplit=1)[0]


def _gen_dml(rng: random.Random) -> str:
    kind = rng.choice(["insert", "update", "delete", "merge"])
    users = RETURNING_POOL["users"]
    posts = RETURNING_POOL["posts"]

    def pick(pool: tuple[str, ...]) -> str:
        return ", ".join(rng.sample(pool, k=rng.randint(1, 4)))

    def plain(pool: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(p for p in pool if p != "merge_action()")

    match kind:
        case "insert":
            email = rng.choice(["NULL", "'new@example.com'"])
            return f"INSERT INTO users (name, email) VALUES ('zed', {email}) RETURNING {pick(plain(users))}"  # ruff: ignore[hardcoded-sql-expression]
        case "update":
            value = rng.choice(["NULL", "'u@example.com'", "name"])
            where = rng.choice(["id = 1", "id < 0", "email IS NULL", "true"])
            return f"UPDATE users SET email = {value} WHERE {where} RETURNING {pick(plain(users))}"  # ruff: ignore[hardcoded-sql-expression]
        case "delete":
            where = rng.choice(["id > 0", "false", "title IS NOT NULL", "user_id = 1"])
            return f"DELETE FROM posts WHERE {where} RETURNING {pick(plain(posts))}"  # ruff: ignore[hardcoded-sql-expression]
    values = rng.choice(["(1, 'm'), (99, 'n')", "(99, 'n')", "(1, 'm')", "(2, NULL)"])
    return (
        f"MERGE INTO users u USING (VALUES {values}) AS v(id, name) ON u.id = v.id "  # ruff: ignore[hardcoded-sql-expression]
        "WHEN MATCHED THEN UPDATE SET email = v.name "
        "WHEN NOT MATCHED THEN INSERT (name) VALUES (coalesce(v.name, 'x')) "
        f"RETURNING {pick(tuple(f'u.{p}' if p in {'id', 'name', 'email'} else p for p in users))}"
    )


def generate(rng: random.Random) -> Generated:
    if rng.random() < 0.15:
        return Generated(sql=_gen_dml(rng))
    shape = _gen_shape(rng)
    return Generated(sql=render(shape), shape=shape)


def reductions(shape: Shape) -> list[Shape]:
    found: list[Shape] = []
    if shape.wrap != "none":
        found.append(replace(shape, wrap="none"))
        if shape.wrap != "subquery":
            found.append(replace(shape, wrap="subquery"))
    if shape.window:
        found.append(replace(shape, window=""))
    if shape.group:
        found.append(replace(shape, group=""))
    if shape.where:
        found.append(replace(shape, where=""))
    if len(shape.columns) > 1:
        found.extend(
            replace(shape, columns=shape.columns[:i] + shape.columns[i + 1 :]) for i in range(len(shape.columns))
        )
    found.extend(replace(shape, joins=shape.joins[:i] + shape.joins[i + 1 :]) for i in range(len(shape.joins)))
    if shape.wrap == "cte_mat":
        found.append(replace(shape, wrap="cte_inline"))
    return found


def minimise(shape: Shape, still_fails: Callable[[str], bool]) -> str:
    current = shape
    progress = True
    while progress:
        progress = False
        for candidate in reductions(current):
            if still_fails(render(candidate)):
                current, progress = candidate, True
                break
    return render(current)


async def _describe_all(dsn: str, statements: list[str]) -> list[list[tuple[bool, str]] | str]:
    conn = await connect_checker(dsn)
    try:
        found: list[list[tuple[bool, str]] | str] = []
        for sql in statements:
            try:
                d = await describe(conn, sql)
            except Exception as error:  # ruff: ignore[blind-except]
                found.append(f"{type(error).__name__}: {error}")
            else:
                found.append([(c.nullable, c.reason) for c in d.columns])
        return found
    finally:
        await conn.close()


def _never_null(conn: psycopg.Connection, sql: str) -> tuple[int, list[bool]]:
    with conn.transaction(force_rollback=True):
        rows = conn.execute(sql.encode()).fetchall()
    width = len(rows[0]) if rows else 0
    return len(rows), [all(row[i] is not None for row in rows) for i in range(width)]


def unsound_columns(dsn: str, sql: str) -> list[int]:
    # a statement that no longer describes or runs is not a reproducer of the same bug
    (verdict,) = asyncio.run(_describe_all(dsn, [sql]))
    if isinstance(verdict, str):
        return []
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            _, never = _never_null(conn, sql)
    except psycopg.Error:
        return []
    return [i for i, (nullable, _) in enumerate(verdict) if not nullable and i < len(never) and not never[i]]


def _record_precision(report: FuzzReport, sql: str, verdict: list[tuple[bool, str]], never: list[bool]) -> None:
    for i, (nullable, reason) in enumerate(verdict):
        if not nullable:
            continue
        counts = report.precision[reason]
        counts.nullable += 1
        if never[i]:
            counts.never_null += 1
            if len(counts.examples) < 3:
                counts.examples.append(f"col {i}: {sql}")


def run_fuzz(dsn: str, seed: int, count: int) -> FuzzReport:
    rng = random.Random(seed)  # ruff: ignore[suspicious-non-cryptographic-random-usage]
    generated = [generate(rng) for _ in range(count)]
    report = FuzzReport(seed=seed, count=count)
    verdicts = asyncio.run(_describe_all(dsn, [g.sql for g in generated]))
    with psycopg.connect(dsn, autocommit=True) as conn:
        for item, verdict in zip(generated, verdicts, strict=True):
            if isinstance(verdict, str):
                report.failures.append(Failure(sql=item.sql, columns=[], reduced=f"describe failed: {verdict}"))
                continue
            try:
                total, never = _never_null(conn, item.sql)
            except psycopg.Error as error:
                report.failures.append(Failure(sql=item.sql, columns=[], reduced=f"execution failed: {error}"))
                continue
            report.statements += 1
            bad = [i for i, (nullable, _) in enumerate(verdict) if not nullable and total and not never[i]]
            if bad:
                reduced = item.sql
                if item.shape is not None:
                    reduced = minimise(item.shape, lambda sql: bool(unsound_columns(dsn, sql)))
                report.failures.append(Failure(sql=item.sql, columns=bad, reduced=reduced))
            if total:
                _record_precision(report, item.sql, verdict, never)
    return report
