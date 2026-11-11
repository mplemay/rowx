# Type mapping

`crates/rowx-postgres/src/types.rs` is the only OID-to-Python table. `rowx_postgres._core.type_table()` exports it, and
`rowx_postgres/_types.py` is the typed view the checker and the row plan read. No other file may hard-code OID to
Python mappings. The generated rows are [Postgres type mapping](../types.md). This page is the rules that table does
not spell out.

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
