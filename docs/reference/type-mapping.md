# Type mapping

`crates/rowx-postgres/src/types.rs` is the only OID-to-Python table. `rowx_postgres._core.type_table()` exports it, and
`rowx_postgres/_types.py` is the typed view the checker and the row plan read. No other file may hard-code OID to
Python mappings. The generated rows are [Type tables](../types.md). This page is the rules that table does not spell
out. Everything down to [SQLite](#sqlite) is PostgreSQL.

Codec dispatch uses the `tokio_postgres::types::Type` from `Statement::params()` and `Column::type_()`, not the bare
OID. tokio-postgres resolves non-builtin OIDs at prepare time. rowx then resolves recursively: a domain uses its base
codec, an enum uses the text-label codec, and an array uses its element codec.

Result columns arrive with the **base** type OID for domains. Parameters and array elements carry the **domain** OID,
so a binary array encode writes the element OID as declared.

## Leaves the annotation chooses

- **Intervals.** `datetime.timedelta` is the default and raises when the value has a non-zero months component.
  `rowx_postgres.Interval` (`months`, `days`, `microseconds`) is a second leaf for the same `interval` column. The
  annotation picks the codec. Parameters accept both `Interval` and `timedelta`.
- **Network.** `IPv4Interface | IPv6Interface` (`inet`) and `IPv4Network | IPv6Network` (`cidr`) are the only
  multi-member unions the [row-type grammar](api.md#row-type-grammar) admits, each as one leaf. An `inet` with a
  full-length mask decodes to an interface whose network is the host. Extraction checks the interface types before
  the address types, because the interfaces are subclasses, and `str` last.
- **Ranges.** `rowx_postgres.Range[T]` is a frozen dataclass with `lower`, `upper`, `lower_inc`, `upper_inc` and
  `empty`. Canonical forms follow the server, so `[1,5]` of `int4range` decodes as `[1, 6)`. `T` must be the
  subtype's leaf. Arrays of ranges are `list[Range[T]]`. A bound whose scalar form is text is still sent in binary
  ([Binary arrays and ranges](driver.md#binary-arrays-and-ranges)).
- **Money.** Results are always binary, so `money` decodes as the int8 minor units `cash_send` writes.
  `rowx_postgres.Money` holds **no scale**, because `lc_monetary` can change mid-session.
  `Money.to_decimal(scale)` takes the scale explicitly. A rendered string is a cast to `text`.
- **`cube`.** `rowx_postgres.Cube(lower_left, upper_right)` keeps both corners. The codec writes the point flag when
  the corners are equal and decodes a point to two equal corners. Corners of different lengths are an `EncodeError`.
- **Geometric types** are fixed-layout binary dataclasses in `rowx_postgres`: `Point`, `Line`, `LineSegment`, `Box`,
  `Path`, `Polygon` and `Circle`. `Path` and `Polygon` hold `tuple[Point, ...]`, and `Path` has a `closed` flag.
- **Extensions** match by name **and** owning extension. Describe resolves `pg_depend` (`deptype = 'e'`) to
  `pg_extension.extname` once per connection and OID, caches that with the statement cache, and the runtime codec
  reuses the map. A same-named type owned by no extension, or by another extension, is `unsupported-type`. `citext`
  uses the same rule. `hstore` is `dict[str, str | None]`. `ltree` and `lquery` are `str`.
- **JSON.** `json` and `jsonb` accept any leaf annotation ([Rules](checker.md#rules)). A bound `str` becomes a JSON
  string; to bind raw JSON text, write `{s}::jsonb`. Values are always `json.dumps`-ed, ASCII-only.
- **Encoding.** Binary wherever the server has a `*_send`/`*_recv` pair, because results are always binary. Scalar
  `numeric`, `timetz`, `json`, `jsonb`, enum labels and `uuid` from `str` may use text as a scalar. Inside an array
  or range those values use binary element encoders. A type with `pg_type.typsend = 0` (for example `aclitem`) cannot
  be read and is `unsupported-type`.

## Decode errors

`DecodeError`, each with a hint to cast to `text`:

- `date`, `timestamp` or `timestamptz` that is ±infinity, or whose year is outside 1..9999;
- `interval` with a non-zero months component when the leaf is `timedelta`, or ±infinity;
- a range whose bounds are ±infinity for a `date` or `timestamp` subtype. A `Range` uses `None` only for an unbounded
  side.

`timetz` stores the zone as seconds **west** of UTC, so the decoder negates it. A naive `time` or `datetime` bound to
an aware column, or the reverse, is an `EncodeError`. `date` does not accept `datetime`. `int` parameters are never
`bool`. An `int` bound to `float4` or `float8` that is larger than 2^53 loses precision. `Decimal('sNaN')` is an
`EncodeError`.

## Unsupported types

Any other type is `unsupported-type` in the checker and `DecodeError` at runtime. Examples: records and composites,
multiranges, `xml`, `tsvector`/`tsquery`, `pg_lsn`, `xid`/`xid8`/`tid`, the `reg*` types, `jsonpath` and `aclitem`.
Records wait for named fields inside a row ([#297](https://github.com/mplemay/rowx/issues/297)), because describe
reports only the `record` pseudo-type. Adding a supported type means appending a row to `types.rs` and regenerating
[the table](../types.md).

## Parameters

1. `None` (SQL NULL) is accepted for every parameter. Parameter nullability is never checked.
2. **Array element nullability** matches SQLx. `list[int]` matches `int4[]`, and `list[int | None]` is also accepted.
   This is the only relaxation of exact matching, and it applies at every nesting level. `str` and `bytes` are never
   treated as sequences.
3. **Extracting parameters.** `bool` is checked before `int`, and `datetime` before `date`, because they are
   subclasses.

## SQLite

SQLite has no column types in the sense PostgreSQL does. Any column can hold any storage class (`INTEGER`, `REAL`,
`TEXT`, `BLOB` or NULL), and the declared type only sets an *affinity*. rowx therefore reads each column by its
**declared type**, which `sqlite3_column_decltype` reports, and checks that every stored value fits it.

`packages/rowx-sqlite/src/types.rs` is the table of kinds (`rowx_sqlite._core.type_table()`), and
`crates/rowx-sqlite/src/types.rs` is the lookup from a declared type to a kind. `rowx_sqlite/_types.py` is the typed
view the checker reads. The generated rows are [Type tables](../types.md#sqlite).

### Lookup

1. The declared type is case-folded and trimmed, and `(n, m)` arguments are removed. If the result is one of the names
   `BOOLEAN`, `DATETIME`, `TIMESTAMP`, `TIMESTAMPTZ`, `DATETIMETZ`, `DATE`, `TIME`, `DECIMAL`, `NUMERIC`, `UUID` or
   `JSON`, that is the kind.
2. Otherwise SQLite's own affinity rules apply, in SQLite's order. A name containing `INT` is `integer`. Then `CHAR`,
   `CLOB` or `TEXT` is `text`, `BLOB` is `blob`, and `REAL`, `FLOA` or `DOUB` is `real`.
3. Any other name has numeric affinity, and the kind is `any`: each value decodes by its storage class.
4. A column with **no declared type** is *untyped*. That is a table column declared without a type, and every
   expression (`count(*)`, `upper(name)`, a literal). It decodes like `any`, and it is always nullable.

The substring rules in step 2 are SQLite's, and they catch names you did not mean: `POINT` and `INTERVAL` contain
`INT`, so they are `integer`, and a stored point or interval raises `DecodeError`. Every name in step 1 has numeric
affinity in SQLite, which matters for the traps below.

A table declared `STRICT` accepts only `INT`, `INTEGER`, `REAL`, `TEXT`, `BLOB` and `ANY` as column types, and SQLite
enforces them. In a strict table the kinds are `integer`, `real`, `text`, `blob` and `any`, and `ANY` decodes by
storage class. A date, a UUID or a decimal in a strict table is `text`. The rich kinds need an ordinary table.

### Reading

A stored value whose storage class disagrees with the column's kind is `DecodeError` and is never coerced. NULL is
`None` for every kind.

| Kind | Reads |
|---|---|
| `integer` | INTEGER only. TEXT is never parsed. |
| `real` | REAL, and an INTEGER that converts exactly (magnitude up to 2^53). `inf` and `-inf` are accepted. |
| `text` | TEXT that is valid UTF-8. Invalid UTF-8 is `DecodeError`. |
| `blob` | BLOB. |
| `boolean` | The INTEGER `0` or `1`. Any other value, `42` included, is `DecodeError`. |
| `datetime` | ISO-8601 text, an INTEGER as Unix seconds, or a REAL as a Julian day. |
| `datetimetz` | The same, with text that carries an offset. |
| `date` | TEXT `YYYY-MM-DD`. |
| `time` | TEXT `HH:MM:SS[.ffffff]`. |
| `decimal` | TEXT exactly, an INTEGER as its digits, or a REAL as the shortest text that names the same value. |
| `uuid` | TEXT, hyphenated, in either case, or a 16-byte BLOB. |
| `json` | TEXT, parsed with `json.loads`. A stored INTEGER or REAL reads as the JSON number it spells. |
| `any` | INTEGER as `int`, REAL as `float`, TEXT as `str`, BLOB as `bytes`. |

Temporal text and the other formats, in detail:

- **Text form.** `YYYY-MM-DD`, then optionally a space or `T` and `HH:MM`, `HH:MM:SS` or `HH:MM:SS.fraction`. A date
  alone is midnight. More than six fraction digits are accepted only when the extra digits are zeros. Years are 1 to
  9999, and anything outside that is `DecodeError`.
- **Offsets.** A `datetime` column rejects text with an offset. A `datetimetz` column requires one (`Z`, `±HH:MM`,
  `±HHMM` or `±HH`), converts to UTC and returns an aware UTC value. A number in a `datetimetz` column is already UTC.
- **Julian days** are rounded to the millisecond, as SQLite computes them.
- **`decimal`** accepts a sign, digits, a fraction and an exponent. NaN, `Infinity`, whitespace and `_` are rejected. A
  REAL is formatted with Rust's shortest round-trip form, which can differ in spelling from Python's `repr` (`1e21`
  and `1e+21`) while naming the same value. A non-finite REAL, which a stored `1e400` becomes, is `DecodeError`.
- **`uuid`** reads 36 hyphenated characters in either case, or exactly 16 bytes.
- **`json`** syntax is checked when the text is parsed, in the binding. A BLOB is SQLite's binary `JSONB`, which rowx
  does not read: it is `DecodeError`. A non-finite REAL is `DecodeError`.
- **`any`** never coerces: it is how a strict `ANY` column and a column with no affinity read.

### Writing

A parameter has no declared type, so the Python type of the value chooses how it is stored:

| Python | Stored |
|---|---|
| `bool` | INTEGER `0` or `1` |
| `int` | INTEGER. A value outside 64 bits is `EncodeError`. |
| `float` | REAL. `nan` is `EncodeError`, because SQLite would store NULL. `inf` is stored. |
| `str` | TEXT. A NUL or a lone surrogate is `EncodeError`. |
| `bytes`, `bytearray`, `memoryview` | BLOB |
| `datetime`, naive | TEXT `YYYY-MM-DD HH:MM:SS[.ffffff]` as it is |
| `datetime`, aware | TEXT `YYYY-MM-DD HH:MM:SS[.ffffff]+00:00`, converted to UTC |
| `date` | TEXT `YYYY-MM-DD` |
| `time` | TEXT `HH:MM:SS[.ffffff]`. An aware `time` is `EncodeError`. |
| `Decimal` | TEXT |
| `UUID` | TEXT, lowercase and hyphenated |
| `dict`, `list`, `tuple` | TEXT from `json.dumps`, with `allow_nan=False`. A value that is not serialisable is `EncodeError`. |
| `None` | NULL |
| anything else | `EncodeError` |

`bool` is checked before `int`, and `datetime` before `date`, because they are subclasses. A parameter is never rejected
for its type, and SQLite then applies the column's affinity when it stores the value: a `str` bound into an `INTEGER`
column that looks like a number is stored as an INTEGER. The checker emits no parameter type mismatch
([Checker](checker.md#sqlite)).

Because a parameter does not know its column, **a naive or aware mismatch is not an `EncodeError` on write**, as it is
on PostgreSQL. A naive `datetime` bound for a `TIMESTAMPTZ` column is stored without an offset, and an aware one bound
for a `TIMESTAMP` column is stored with `+00:00`. Both surface as `DecodeError` when the row is read. Bind an aware
value for a `TIMESTAMPTZ` column and a naive one for a `TIMESTAMP` column.

An `EncodeError` carries a note that names the parameter (`while binding parameter ?2`). A statement with more
parameters than `SQLITE_LIMIT_VARIABLE_NUMBER` is `InterfaceError`. A query object fails earlier, when it compiles,
with the `TypeError` of [Compile limits](api.md#compile-limits) above 32766 parameters.

### Affinity traps

- **`DECIMAL` and `NUMERIC` are lossy.** The names have numeric affinity, so SQLite converts the text `'1.50'` to the
  REAL `1.5` when it stores it. The `Decimal` that is read is `1.5`, and a value with more than about 15 significant
  digits loses its tail. To keep exact text, declare the column `TEXT` and read a `str`. There is no column type
  override ([#302](https://github.com/mplemay/rowx/issues/302)).
- **`JSON` normalises bare numbers.** The same affinity turns the text `1.0` into the INTEGER `1`, so it reads back as
  `1`, and a large integer can lose precision. `1e400` overflows to infinity and fails to decode. JSON objects and
  arrays are stored as TEXT and are unchanged.
- **`POINT`, `INTERVAL` and any name containing `INT`** are integers (above).
- **A value that does not fit raises.** A date column holding the text `'tomorrow'` is `DecodeError` when read. The
  checker cannot see stored data, only declarations.
