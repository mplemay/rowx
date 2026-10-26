# Type mapping (single source of truth)

`crates/rowx-postgres/src/types.rs` defines one static table of `TypeInfo` rows. `rowx._core.postgres`
exports it as `type_table() -> list[TypeInfo]`. `rowx/postgres/_types.py` wraps it, and both the
checker and `_rowplan` read from there. **No other file may hard-code OID↔Python
mappings.**

```rust
pub struct TypeInfo {
    pub key: &'static str,                 // "int4", "timestamptz", "enum", "citext", "array", ...
    pub matcher: Matcher,                  // Oid(u32) | EnumKind | NamedSimple("citext") | ExtNamed(type, extension) | RangeKind | ArrayKind | DomainKind
    pub py_result: &'static str,           // "int", "decimal.Decimal", "datetime.datetime", ...; "" if unsupported
    pub tz: TzRule,                        // None | Aware | Naive   (documentation + encode checks)
    pub accepted_params: &'static [&'static str],   // Python type names the encoder & checker accept
    pub decode: Codec, pub encode: Codec,  // enum variant per family; Unsupported allowed
    pub encode_format: Format,             // Binary | Text (default per row; arrays decide per value)
}
```

Codec dispatch uses the `tokio_postgres::types::Type` taken from `Statement::params()` and
`Column::type_()`, not the bare OID. tokio-postgres already resolves non-builtin OIDs at
prepare time into `Kind::{Enum, Domain, Array, …}`. rowx resolves recursively:

- `Domain(b)` uses the codec of `b`;
- `Enum` uses the text-label codec;
- `Array(e)` uses the codec of `e`.

Result columns arrive with the **base** type OID for domains. Parameters and array elements
carry the **domain** OID, so a binary array encode writes `e.oid()`, the element OID as
declared.

| Postgres type | Result type (checker leaf) | Decode | Accepted param types (checker = runtime) | Encode |
|---|---|---|---|---|
| `bool` | `bool` | binary | `bool` | binary |
| `int2`, `int4`, `int8` | `int` | binary | `int` (never `bool`) | binary, range-checked → `EncodeError` |
| `oid` | `int` | binary | `int` | binary |
| `float4`, `float8` | `float` | binary | `float`, `int` | binary (`int` > 2^53 loses precision; documented) |
| `numeric` | `decimal.Decimal` | binary → Decimal (NaN, ±Infinity) | `Decimal`, `int` | text (`Decimal('sNaN')` → `EncodeError`) |
| `text`, `varchar`, `bpchar`, `name`, `citext` | `str` | binary UTF-8 | `str` **only** | binary |
| `"char"` (OID 18) | `str` | binary (1 byte) | `str` (len 1) | binary |
| `bytea` | `bytes` | binary | `bytes`, `bytearray`, `memoryview` | binary |
| `date` | `datetime.date` | binary | `date` (not `datetime`) | binary |
| `time` | `datetime.time` (naive) | binary | naive `time` | binary |
| `timetz` | `datetime.time` (aware) | binary; the zone is stored as seconds **west** of UTC, so negate it | aware `time` (naive → `EncodeError`) | text |
| `timestamp` | `datetime.datetime` (naive) | binary | naive `datetime` (aware → `EncodeError`) | binary |
| `timestamptz` | `datetime.datetime` (UTC-aware) | binary | aware `datetime` (naive → `EncodeError`) | binary |
| `interval` | `datetime.timedelta` | binary | `timedelta` | binary |
| `interval` with `Interval` leaf | `rowx.postgres.Interval` (`months`, `days`, `microseconds`) | binary, lossless | `Interval` | binary |
| `inet` | `ipaddress.IPv4Interface \| ipaddress.IPv6Interface` (one leaf) | binary | `IPv4Interface`, `IPv6Interface`, `IPv4Address`, `IPv6Address`, `str` | binary (`str` is parsed by `ipaddress` first) |
| `cidr` | `ipaddress.IPv4Network \| ipaddress.IPv6Network` (one leaf) | binary | `IPv4Network`, `IPv6Network`, `str` | binary |
| `macaddr`, `macaddr8` | `str` (`08:00:2b:01:02:03`, lower case) | binary | `str` | binary |
| `int4range`, `int8range`, `numrange`, `daterange`, `tsrange`, `tstzrange` | `rowx.postgres.Range[T]`, with `T` the leaf of the subtype | binary | `Range[T]` with `T` accepted by the subtype | binary |
| `money` | `rowx.postgres.Money` (`minor_units: int`; no scale) | binary (int8) | `Money` | binary |
| `bit(n)`, `varbit` | `str` of `0` and `1` | binary | `str` of `0` and `1` (`bit(n)` length checked by the server) | binary |
| `point`, `line`, `lseg`, `box`, `path`, `polygon`, `circle` | frozen dataclasses `Point`, `Line`, `LineSegment`, `Box`, `Path`, `Polygon`, `Circle` in `rowx.postgres` | binary | the matching dataclass | binary |
| `hstore` (extension) | `dict[str, str \| None]` | binary | `dict[str, str \| None]` | binary |
| `ltree`, `lquery` (extensions) | `str` | binary | `str` | binary |
| `cube` (extension) | `rowx.postgres.Cube` (`lower_left` and `upper_right`, each a `tuple[float, ...]` of the same length; a point has equal corners) | binary | `Cube` | binary |
| `uuid` | `uuid.UUID` | binary | `UUID`, `str` | binary (`UUID`), text (`str`) |
| `json`, `jsonb` | any annotation ([Rules](checker.md#rules-all-errors)) | `json.loads` (jsonb: strip the `0x01` version byte) | any JSON-serialisable value, always `json.dumps`-ed. A `str` becomes a JSON **string**; to bind raw JSON text, write `{s}::jsonb` | text |
| enum | `str` | binary (the label) | `str` | text |
| domain | as its base type | as its base type | as its base type | as its base type |
| `X[]` | `list[X']`; nested lists for multi-dim | binary arrays | `list`/`tuple` of accepted X (`str`/`bytes` are never sequences) | binary if every element encodes binary, else a text literal ([Codec](driver.md#codec-rowx-postgres-pysrccodec-rowx-postgressrcwire)) |
| `void` | `None` (tuple element only; [Rules](checker.md#rules-all-errors)) | → `None` | – | – |

Decode errors (`DecodeError`, each with a hint to cast to `text`):

- `date`, `timestamp` or `timestamptz` that is ±infinity, or whose year is outside 1..9999;
- `interval` with a non-zero months component (when the leaf is `timedelta`), or ±infinity;
- a range whose bounds are ±infinity for a `date` or `timestamp` subtype (a `Range` keeps `None` for an unbounded side
  only).

**Decisions for the added families** ([decision log](decision-log.md#type-parity-98)).

- **Intervals.** The leaf `datetime.timedelta` stays the default and raises on months. `rowx.postgres.Interval` is a
  second leaf for the same `interval` column. The annotation picks the codec, and the checker accepts both. The
  parameter side accepts `Interval` and `timedelta`.
- **Network.** `IPv4Interface | IPv6Interface` and `IPv4Network | IPv6Network` are the only multi-member unions the
  grammar admits, each as one leaf. An `inet` with a full-length mask decodes to an interface whose `network` is the
  host. Parameter extraction order: `IPv4Interface`, `IPv6Interface` (subclasses of the address types) are checked
  before `IPv4Address` and `IPv6Address`, and `str` last.
- **Ranges.** `Range[T]` is `@dataclass(frozen=True, slots=True)` with `lower`, `upper`, `lower_inc`, `upper_inc` and
  an `empty` flag, modelled on psycopg. Canonical forms follow the server, so `[1,5]` of `int4range` decodes as
  `[1, 6)`. `T` must equal the subtype's leaf. Arrays of ranges are `list[Range[T]]`.
- **Money.** The result format is always binary ([Driver](driver.md)),
  so `money` decodes as the int8 minor units that `cash_send` writes. rowx holds **no scale**, because `lc_monetary` can
  change mid-session. `Money.to_decimal(scale: int)` takes the scale explicitly, so the caller owns the
  interpretation, as with sqlx's `PgMoney`. A caller who wants a rendered string casts to `text`.
- **`cube`.** `Cube(lower_left, upper_right)` keeps both corners and the dimension. The codec writes the point flag
  when the corners are equal and decodes a point to two equal corners, so a point and a box are never confused. Corners
  of different lengths are an `EncodeError`.
- **Geometric types** are fixed-layout binary. `Path` and `Polygon` hold `tuple[Point, ...]`, and `Path`
  has a `closed` flag.
- **Extension types** match by name **and** owner. A `NamedSimple` row also carries the extension name. The describe
  step (`describe.rs`) resolves, once per connection and OID, `pg_depend` (`deptype = 'e'`) → `pg_extension.extname`,
  and caches it with the statement cache, so it is dropped with it. The same resolved map feeds runtime codec dispatch,
  which never queries on its own. A same-named type owned by no extension, or by another extension, is
  `unsupported-type`. `citext` gets the same rule.
- **Wiring.** `types.rs` gains `Matcher::ExtNamed(type, extension)` and `Matcher::RangeKind`, and `Codec` variants
  `Network`, `Mac`, `Range`, `Money`, `Bits`, `Geometric` and `Extension` (one per module under `wire/` and `codec/`).
  The `Interval` leaf is a second `py_result` form on the `interval` row. These are enum changes, so the feature PRs
  (#99–#103) call them out as wiring.
- **Encoding format.** Binary wherever the server has a `*_send`/`*_recv` pair, because results are always binary, so a
  type with no binary I/O cannot be read at all. The `typsend = 0` rule below still applies, and an extension type
  without binary I/O stays `unsupported-type`.

**Unsupported types.** Any other type is `unsupported-type` in the checker and `DecodeError`
at runtime. Examples: records and composites, multiranges, `xml`, `tsvector`/`tsquery`, `pg_lsn`, `xid`/`xid8`/`tid`,
the `reg*` types, `jsonpath` and `aclitem`. Records wait for named row types ([roadmap](roadmap.md) item 1), because
describe reports only the `record` pseudo-type.

- A type with no binary send function (`pg_type.typsend = 0`, e.g. `aclitem`) makes the
  whole query fail at Bind with 42883. The checker therefore also reports these as
  `unsupported-type` ([Interface and checker session](nullability.md#interface-and-checker-session)).
- Adding a type later means appending a row to the table.

Notes:

1. `None` (SQL NULL) is accepted for every parameter. Parameter nullability is never
   checked.
2. **Array element nullability** works as in SQLx. `list[int]` matches `int4[]`, and
   `list[int | None]` is also accepted; this is the only relaxation of exact matching. The
   rule applies at every nesting level.
3. **Extracting parameters.** `bool` is checked before `int`, and `datetime` before `date`,
   because they are subclasses.
