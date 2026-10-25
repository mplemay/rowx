# Type mapping (single source of truth)

`crates/rowx-pg/src/types.rs` defines one static table of `TypeInfo` rows. `rowx._core`
exports it as `type_table() -> list[TypeInfo]`. `rowx/_types.py` wraps it, and both the
checker and `_rowplan` read from there. **No other file may hard-code OID↔Python
mappings.**

```rust
pub struct TypeInfo {
    pub key: &'static str,                 // "int4", "timestamptz", "enum", "citext", "array", ...
    pub matcher: Matcher,                  // Oid(u32) | EnumKind | NamedSimple("citext") | ArrayKind | DomainKind
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
| `uuid` | `uuid.UUID` | binary | `UUID`, `str` | binary (`UUID`), text (`str`) |
| `json`, `jsonb` | any annotation ([Rules](checker.md#rules-all-errors)) | `json.loads` (jsonb: strip the `0x01` version byte) | any JSON-serialisable value, always `json.dumps`-ed. A `str` becomes a JSON **string**; to bind raw JSON text, write `{s}::jsonb` | text |
| enum | `str` | binary (the label) | `str` | text |
| domain | as its base type | as its base type | as its base type | as its base type |
| `X[]` | `list[X']`; nested lists for multi-dim | binary arrays | `list`/`tuple` of accepted X (`str`/`bytes` are never sequences) | binary if every element encodes binary, else a text literal ([Codec](driver.md#codec-rowx-pysrccodec-rowx-pgsrcwire)) |
| `void` | `None` (tuple element only; [Rules](checker.md#rules-all-errors)) | → `None` | – | – |

Decode errors (`DecodeError`, each with a hint to cast to `text`):

- `date`, `timestamp` or `timestamptz` that is ±infinity, or whose year is outside 1..9999;
- `interval` with a non-zero months component, or ±infinity.

**Unsupported types.** Any other type is `unsupported-type` in the checker and `DecodeError`
at runtime. Examples: `money`, `inet`/`cidr`, ranges, composites, `xml`, `hstore`, `regclass`,
`aclitem`.

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
