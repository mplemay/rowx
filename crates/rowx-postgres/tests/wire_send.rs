// Compares rowx's wire codecs with the server's own `*_send` functions. Every value is written as a
// SQL text literal, so the server derives the expected bytes without going through a rowx encoder.
pub mod common;

use std::fmt::Write;

use common::TestResult;
use proptest::prelude::*;
use proptest::strategy::{Strategy, ValueTree};
use proptest::test_runner::TestRunner;
use rowx_postgres::exec::Param;
use rowx_postgres::wire::array;
use rowx_postgres::wire::mac::{self, MAC_LEN, MAC8_LEN};
use rowx_postgres::wire::network::{self, Addr, Network};
use rowx_postgres::wire::numeric::{self, Numeric};
use rowx_postgres::wire::temporal::{self, Date, TimeOfDay};
use tokio_postgres::Client;

const CASES: usize = 64;
const MICROS_PER_SECOND: i64 = 1_000_000;
const MICROS_PER_DAY: i64 = 86_400 * MICROS_PER_SECOND;

fn hex(bytes: &[u8]) -> String {
    bytes.iter().fold(String::new(), |mut out, byte| {
        let _ = write!(out, "{byte:02x}");
        out
    })
}

fn samples<S: Strategy>(strategy: S, count: usize) -> Vec<S::Value> {
    let mut runner = TestRunner::deterministic();
    (0..count)
        .filter_map(|_| strategy.new_tree(&mut runner).ok())
        .map(|tree| tree.current())
        .collect()
}

// `<func>($1::text::<ty>)`, the bytes the server itself would send for the literal
async fn send(client: &Client, func: &str, ty: &str, literal: &str) -> TestResult<Vec<u8>> {
    let sql = format!("SELECT {func}($1::text::{ty})");
    Ok(client.query_one(&sql, &[&literal]).await?.get(0))
}

// the server's text rendering of a parameter that rowx encoded
async fn read_back(client: &Client, ty: &str, param: Param) -> TestResult<String> {
    let sql = format!("SELECT $1::{ty}::text");
    Ok(client.query_one(&sql, &[&param]).await?.get(0))
}

async fn literal_text(client: &Client, ty: &str, literal: &str) -> TestResult<String> {
    let sql = format!("SELECT $1::text::{ty}::text");
    Ok(client.query_one(&sql, &[&literal]).await?.get(0))
}

macro_rules! connect {
    () => {{
        let Some(schema) = common::fresh_schema().await? else {
            return Ok(());
        };
        let client = schema.connect().await?;
        (schema, client)
    }};
}

const fn is_leap(year: i32) -> bool {
    (year % 4 == 0 && year % 100 != 0) || year % 400 == 0
}

const fn days_in_month(year: i32, month: u32) -> u32 {
    match month {
        2 if is_leap(year) => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    }
}

fn civil_date() -> impl Strategy<Value = (i32, u32, u32)> {
    let edges = prop_oneof![
        Just((1, 1, 1)),
        Just((9999, 12, 31)),
        Just((2000, 1, 1)),
        Just((1999, 12, 31)),
        Just((2000, 2, 29)),
        Just((1900, 2, 28)),
    ];
    let any_day = (1i32..=9999, 1u32..=12, 1u32..=31)
        .prop_map(|(year, month, day)| (year, month, day.min(days_in_month(year, month))));
    prop_oneof![1 => edges, 4 => any_day]
}

fn date_text((year, month, day): (i32, u32, u32)) -> String {
    format!("{year:04}-{month:02}-{day:02}")
}

fn time_text(micros: i64) -> String {
    let seconds = micros / MICROS_PER_SECOND;
    format!(
        "{:02}:{:02}:{:02}.{:06}",
        seconds / 3600,
        seconds / 60 % 60,
        seconds % 60,
        micros % MICROS_PER_SECOND
    )
}

fn micros_of_day() -> impl Strategy<Value = i64> {
    prop_oneof![
        1 => Just(0i64),
        1 => Just(MICROS_PER_DAY - 1),
        4 => 0..MICROS_PER_DAY,
    ]
}

fn offset_text(offset_seconds: i64) -> String {
    let sign = if offset_seconds < 0 { '-' } else { '+' };
    let abs = offset_seconds.abs();
    format!(
        "{sign}{:02}:{:02}:{:02}",
        abs / 3600,
        abs / 60 % 60,
        abs % 60
    )
}

fn time_of(micros: i64) -> TimeOfDay {
    let seconds = micros / MICROS_PER_SECOND;
    TimeOfDay {
        hour: u8::try_from(seconds / 3600).unwrap_or(0),
        minute: u8::try_from(seconds / 60 % 60).unwrap_or(0),
        second: u8::try_from(seconds % 60).unwrap_or(0),
        microsecond: u32::try_from(micros % MICROS_PER_SECOND).unwrap_or(0),
    }
}

fn date_of((year, month, day): (i32, u32, u32)) -> Date {
    Date {
        year: u16::try_from(year).unwrap_or(0),
        month: u8::try_from(month).unwrap_or(0),
        day: u8::try_from(day).unwrap_or(0),
    }
}

#[tokio::test]
async fn fixed_width_scalars_match_the_server_send_functions() -> TestResult {
    let (schema, client) = connect!();
    for value in samples(any::<bool>(), 2) {
        let sent = send(&client, "boolsend", "bool", &value.to_string()).await?;
        assert_eq!(sent, vec![u8::from(value)], "bool {value}");
    }
    for value in samples(
        prop_oneof![Just(i16::MIN), Just(i16::MAX), any::<i16>()],
        CASES,
    ) {
        let sent = send(&client, "int2send", "int2", &value.to_string()).await?;
        assert_eq!(sent, value.to_be_bytes().to_vec(), "int2 {value}");
    }
    for value in samples(
        prop_oneof![Just(i32::MIN), Just(i32::MAX), any::<i32>()],
        CASES,
    ) {
        let sent = send(&client, "int4send", "int4", &value.to_string()).await?;
        assert_eq!(sent, value.to_be_bytes().to_vec(), "int4 {value}");
    }
    for value in samples(
        prop_oneof![Just(i64::MIN), Just(i64::MAX), any::<i64>()],
        CASES,
    ) {
        let sent = send(&client, "int8send", "int8", &value.to_string()).await?;
        assert_eq!(sent, value.to_be_bytes().to_vec(), "int8 {value}");
    }
    for value in samples(any::<u32>(), CASES) {
        let sent = send(&client, "oidsend", "oid", &value.to_string()).await?;
        assert_eq!(sent, value.to_be_bytes().to_vec(), "oid {value}");
    }
    schema.drop().await
}

fn finite_f32() -> impl Strategy<Value = f32> {
    proptest::num::f32::NORMAL
        | proptest::num::f32::SUBNORMAL
        | proptest::num::f32::ZERO
        | proptest::num::f32::INFINITE
}

fn finite_f64() -> impl Strategy<Value = f64> {
    proptest::num::f64::NORMAL
        | proptest::num::f64::SUBNORMAL
        | proptest::num::f64::ZERO
        | proptest::num::f64::INFINITE
}

#[tokio::test]
async fn floats_match_the_server_send_functions() -> TestResult {
    let (schema, client) = connect!();
    for value in samples(finite_f32(), CASES) {
        let sent = send(&client, "float4send", "float4", &value.to_string()).await?;
        assert_eq!(sent, value.to_be_bytes().to_vec(), "float4 {value:e}");
    }
    for value in samples(finite_f64(), CASES) {
        let sent = send(&client, "float8send", "float8", &value.to_string()).await?;
        assert_eq!(sent, value.to_be_bytes().to_vec(), "float8 {value:e}");
    }
    let nan4 = send(&client, "float4send", "float4", "NaN").await?;
    assert!(f32::from_be_bytes(<[u8; 4]>::try_from(nan4.as_slice())?).is_nan());
    let nan8 = send(&client, "float8send", "float8", "NaN").await?;
    assert!(f64::from_be_bytes(<[u8; 8]>::try_from(nan8.as_slice())?).is_nan());
    schema.drop().await
}

#[tokio::test]
async fn text_bytea_and_uuid_match_the_server_send_functions() -> TestResult {
    let (schema, client) = connect!();
    // NUL cannot be stored in text
    for value in samples("[^\\x00]{0,24}", CASES) {
        let sent = send(&client, "textsend", "text", &value).await?;
        assert_eq!(sent, value.as_bytes(), "text {value:?}");
    }
    for value in samples(prop::collection::vec(any::<u8>(), 0..32), CASES) {
        let hex: String = hex(&value);
        let sent = send(&client, "byteasend", "bytea", &format!("\\x{hex}")).await?;
        assert_eq!(sent, value, "bytea {hex}");
    }
    for value in samples(any::<[u8; 16]>(), CASES) {
        let hex: String = hex(&value);
        let sent = send(&client, "uuid_send", "uuid", &hex).await?;
        assert_eq!(sent, value.to_vec(), "uuid {hex}");
    }
    schema.drop().await
}

#[tokio::test]
async fn date_matches_date_send() -> TestResult {
    let (schema, client) = connect!();
    for ymd in samples(civil_date(), CASES) {
        let text = date_text(ymd);
        let sent = send(&client, "date_send", "date", &text).await?;
        assert_eq!(
            temporal::encode_date(ymd.0, ymd.1, ymd.2),
            Ok(sent.clone()),
            "{text}"
        );
        assert_eq!(temporal::decode_date(&sent), Ok(date_of(ymd)), "{text}");
        let back = read_back(&client, "date", Param::binary(sent)).await?;
        assert_eq!(back, text);
    }
    schema.drop().await
}

#[tokio::test]
async fn time_matches_time_send() -> TestResult {
    let (schema, client) = connect!();
    for micros in samples(micros_of_day(), CASES) {
        let text = time_text(micros);
        let sent = send(&client, "time_send", "time", &text).await?;
        assert_eq!(temporal::encode_time(micros), sent, "{text}");
        assert_eq!(temporal::decode_time(&sent), Ok(time_of(micros)), "{text}");
        let back = read_back(&client, "time", Param::binary(sent)).await?;
        assert_eq!(back, literal_text(&client, "time", &text).await?);
    }
    schema.drop().await
}

#[tokio::test]
async fn timetz_text_is_read_back_by_the_server() -> TestResult {
    let (schema, client) = connect!();
    let cases = samples((micros_of_day(), -57_599i64..=57_599), CASES);
    for (micros, offset) in cases {
        let text = temporal::encode_timetz_text(micros, offset * MICROS_PER_SECOND)?;
        let expected = format!("{}{}", time_text(micros), offset_text(offset));
        assert_eq!(text, expected);
        // the server resolves the zone sign, and rowx's decoder must agree with what it stored
        let sent = send(&client, "timetz_send", "timetz", &text).await?;
        let decoded = temporal::decode_timetz(&sent)?;
        assert_eq!(decoded.time, time_of(micros), "{text}");
        assert_eq!(i64::from(decoded.utc_offset), offset, "{text}");
        let shown = read_back(&client, "timetz", Param::text(text.clone().into_bytes())).await?;
        let canonical = literal_text(&client, "timetz", &text).await?;
        assert_eq!(shown, canonical, "{text}");
    }
    schema.drop().await
}

#[tokio::test]
async fn timestamp_matches_timestamp_send() -> TestResult {
    let (schema, client) = connect!();
    for (ymd, micros) in samples((civil_date(), micros_of_day()), CASES) {
        let text = format!("{} {}", date_text(ymd), time_text(micros));
        let sent = send(&client, "timestamp_send", "timestamp", &text).await?;
        let ours = temporal::encode_timestamp(ymd.0, ymd.1, ymd.2, micros, 0);
        assert_eq!(ours, sent, "{text}");
        let decoded = temporal::decode_timestamp("timestamp", &sent)?;
        assert_eq!(
            (decoded.date, decoded.time),
            (date_of(ymd), time_of(micros)),
            "{text}"
        );
        let back = read_back(&client, "timestamp", Param::binary(sent)).await?;
        assert_eq!(back, literal_text(&client, "timestamp", &text).await?);
    }
    schema.drop().await
}

#[tokio::test]
async fn timestamptz_matches_timestamptz_send() -> TestResult {
    let (schema, client) = connect!();
    let cases = samples((civil_date(), micros_of_day(), -57_599i64..=57_599), CASES);
    for (ymd, micros, offset) in cases {
        let text = format!(
            "{} {}{}",
            date_text(ymd),
            time_text(micros),
            offset_text(offset)
        );
        let sent = send(&client, "timestamptz_send", "timestamptz", &text).await?;
        let ours =
            temporal::encode_timestamp(ymd.0, ymd.1, ymd.2, micros, offset * MICROS_PER_SECOND);
        assert_eq!(ours, sent, "{text}");
    }
    // at UTC the decoded fields are the literal's own
    for (ymd, micros) in samples((civil_date(), micros_of_day()), CASES) {
        let text = format!("{} {}+00:00:00", date_text(ymd), time_text(micros));
        let sent = send(&client, "timestamptz_send", "timestamptz", &text).await?;
        let decoded = temporal::decode_timestamp("timestamptz", &sent)?;
        assert_eq!(
            (decoded.date, decoded.time),
            (date_of(ymd), time_of(micros)),
            "{text}"
        );
    }
    schema.drop().await
}

#[tokio::test]
async fn interval_matches_interval_send() -> TestResult {
    let (schema, client) = connect!();
    let micros_range = -1_000_000_000_000_000i64..=1_000_000_000_000_000;
    for (days, micros) in samples((any::<i32>(), micros_range), CASES) {
        let text = format!("{days} days {micros} microseconds");
        let sent = send(&client, "interval_send", "interval", &text).await?;
        assert_eq!(
            temporal::encode_interval(i64::from(days), micros),
            Ok(sent.clone()),
            "{text}"
        );
        let decoded = temporal::decode_interval(&sent)?;
        assert_eq!(
            (decoded.days, decoded.microseconds),
            (days, micros),
            "{text}"
        );
    }
    schema.drop().await
}

fn numeric_literal() -> impl Strategy<Value = String> {
    let specials = prop_oneof![
        Just("0".to_owned()),
        Just("-0".to_owned()),
        Just("NaN".to_owned()),
        Just("Infinity".to_owned()),
        Just("-Infinity".to_owned()),
        Just("0.0000".to_owned()),
        Just("1e-30".to_owned()),
        Just("1E+2".to_owned()),
        Just("99999999999999999999999999999999999999".to_owned()),
        Just("0.0001".to_owned()),
        Just("10000".to_owned()),
        Just("100000000".to_owned()),
    ];
    let plain = "-?[0-9]{1,30}(\\.[0-9]{1,40})?";
    // scales near the 1000-digit display limit and weights at both ends of the range
    let small = (1usize..=1000).prop_map(|scale| format!("0.{}1", "0".repeat(scale - 1)));
    let large = (1usize..=100_000).prop_map(|zeros| format!("1{}", "0".repeat(zeros)));
    prop_oneof![2 => specials, 6 => plain, 1 => small, 1 => large]
}

#[tokio::test]
async fn numeric_matches_numeric_send() -> TestResult {
    let (schema, client) = connect!();
    for literal in samples(numeric_literal(), CASES * 2) {
        let shown = if literal.len() > 40 {
            format!("{}... ({} chars)", &literal[..20], literal.len())
        } else {
            literal.clone()
        };
        let sent = send(&client, "numeric_send", "numeric", &literal).await?;
        let decoded = numeric::decode(&sent)?;
        // the decoder's text is the server's own rendering of the same value
        let server_text = literal_text(&client, "numeric", &literal).await?;
        assert_eq!(decoded.to_decimal_string(), server_text, "{shown}");
        // and re-encoding what the server sent gives the server's bytes back; for NaN and the infinities
        // the server fills the display scale with a stray value, so only the other fields are compared
        let reencoded = decoded.encode()?;
        if matches!(decoded, Numeric::Finite { .. }) {
            assert_eq!(reencoded, sent, "{shown}");
        } else {
            assert_eq!(reencoded[..6], sent[..6], "{shown}");
        }
        let back = read_back(&client, "numeric", Param::binary(sent)).await?;
        assert_eq!(back, server_text, "{shown}");
    }
    schema.drop().await
}

#[tokio::test]
async fn numeric_text_parameters_are_read_back_by_the_server() -> TestResult {
    let (schema, client) = connect!();
    // what Python's str(Decimal) can produce, mapped by the encoder before it reaches the server
    let inputs = [
        "1E+2",
        "-1E-7",
        "0E-10",
        "NaN",
        "-NaN",
        "NaN123",
        "Infinity",
        "-Infinity",
        "0.000",
        "123456789012345678901234567890.123456789",
        "-0",
        "1E+30",
    ];
    for input in inputs {
        let text = numeric::normalize_decimal_text(input)?;
        let shown = read_back(&client, "numeric", Param::text(text.clone().into_bytes())).await?;
        let expected = literal_text(&client, "numeric", &text).await?;
        assert_eq!(shown, expected, "{input}");
        let sent = send(&client, "numeric_send", "numeric", &text).await?;
        assert_eq!(
            numeric::decode(&sent)?.to_decimal_string(),
            expected,
            "{input}"
        );
    }
    // a signaling NaN never reaches the server
    assert!(numeric::normalize_decimal_text("sNaN").is_err());
    // binary Numeric values built by hand are accepted by numeric_recv
    let value = Numeric::Finite {
        negative: true,
        weight: 1,
        scale: 4,
        digits: vec![1, 2345, 6700],
    };
    let back = read_back(&client, "numeric", Param::binary(value.encode()?)).await?;
    assert_eq!(back, "-12345.6700");
    schema.drop().await
}

#[tokio::test]
async fn json_and_jsonb_text_parameters_are_read_back_by_the_server() -> TestResult {
    let (schema, client) = connect!();
    let documents = [
        r#"{"a": [1, 2.5, null, true], "b": {"c": "d"}}"#,
        r"[]",
        r#""just a string""#,
        r"123",
        r"null",
        r#"{"quote": "\"", "slash": "\\", "unicode": "\u00e9\ud83d\ude00"}"#,
    ];
    for document in documents {
        // json keeps the text verbatim
        let shown = read_back(&client, "json", Param::text(document.as_bytes().to_vec())).await?;
        assert_eq!(shown, document);
        // jsonb_send is a version byte followed by the canonical text
        let sent = send(&client, "jsonb_send", "jsonb", document).await?;
        assert_eq!(sent.first(), Some(&1));
        let parsed: serde_json::Value = serde_json::from_slice(&sent[1..])?;
        assert_eq!(
            parsed,
            serde_json::from_str::<serde_json::Value>(document)?,
            "{document}"
        );
        let shown = read_back(&client, "jsonb", Param::text(document.as_bytes().to_vec())).await?;
        assert_eq!(shown.parse::<serde_json::Value>()?, parsed, "{document}");
    }
    schema.drop().await
}

#[tokio::test]
async fn enum_labels_are_read_back_by_the_server() -> TestResult {
    let (schema, client) = connect!();
    for label in ["sad", "ok", "happy"] {
        let shown = read_back(&client, "mood", Param::text(label.as_bytes().to_vec())).await?;
        assert_eq!(shown, label);
    }
    assert!(
        read_back(&client, "mood", Param::text(b"nope".to_vec()))
            .await
            .is_err()
    );
    schema.drop().await
}

// a rectangular shape with a flat, row-major element list of text values (`None` is NULL)
fn shaped(
    element: impl Strategy<Value = String> + Clone + 'static,
) -> impl Strategy<Value = (Vec<usize>, Vec<Option<String>>)> {
    prop::collection::vec(0usize..4, 0..=3).prop_flat_map(move |dims| {
        let total = if dims.is_empty() {
            0
        } else {
            dims.iter().product()
        };
        let elements = prop::collection::vec(prop::option::of(element.clone()), total);
        (Just(dims), elements)
    })
}

fn borrowed(elements: &[Option<Vec<u8>>]) -> Vec<Option<&[u8]>> {
    elements.iter().map(Option::as_deref).collect()
}

#[tokio::test]
async fn int_arrays_match_array_send() -> TestResult {
    let (schema, client) = connect!();
    let cases = samples(shaped(any::<i32>().prop_map(|v| v.to_string())), CASES);
    for (dims, elements) in cases {
        let texts: Vec<Option<Vec<u8>>> = elements
            .iter()
            .map(|e| e.as_ref().map(|s| s.clone().into_bytes()))
            .collect();
        let empty = dims.is_empty() || dims.contains(&0);
        // encode_literal is only for non-empty shapes
        let literal = if empty {
            "{}".to_owned()
        } else {
            array::encode_literal(&dims, &borrowed(&texts))
        };
        let sent = send(&client, "array_send", "int4[]", &literal).await?;
        let binary: Vec<Option<Vec<u8>>> = elements
            .iter()
            .map(|e| {
                e.as_ref()
                    .map(|s| s.parse::<i32>().map(|v| v.to_be_bytes().to_vec()))
                    .transpose()
            })
            .collect::<Result<_, _>>()?;
        let ours = array::encode(23, &dims, &borrowed(&binary))?;
        // a shape with a zero-length dimension is the zero-dimensional empty array on the server
        let expected = if empty {
            array::encode(23, &[], &[])?
        } else {
            ours.clone()
        };
        assert_eq!(sent, expected, "{literal}");
        let decoded = array::decode(&sent)?;
        assert_eq!(decoded.element_oid, 23, "{literal}");
        if !empty {
            assert_eq!(decoded.dims, dims, "{literal}");
            assert_eq!(decoded.elements, borrowed(&binary), "{literal}");
        }
        // the binary form rowx sends parses on the server and prints like the literal does
        let shown = read_back(&client, "int4[]", Param::binary(ours)).await?;
        let canonical = literal_text(&client, "int4[]", &literal).await?;
        assert_eq!(shown, canonical, "{literal}");
    }
    schema.drop().await
}

#[tokio::test]
async fn arrays_with_odd_bounds_decode_to_their_dimensions() -> TestResult {
    let (schema, client) = connect!();
    let cases: [(&str, Vec<usize>, usize); 5] = [
        ("'[0:2]={1,2,3}'::int4[]", vec![3], 3),
        ("'[5:6]={1,2}'::int4[]", vec![2], 2),
        ("'[-3:-2][1:2]={{1,2},{3,4}}'::int4[]", vec![2, 2], 4),
        ("'{}'::int4[]", vec![], 0),
        ("'{{1,NULL},{3,4}}'::int4[]", vec![2, 2], 4),
    ];
    for (expr, dims, count) in cases {
        let row = client
            .query_one(&format!("SELECT array_send({expr})"), &[])
            .await?;
        let raw: Vec<u8> = row.get(0);
        let decoded = array::decode(&raw)?;
        assert_eq!(decoded.dims, dims, "{expr}");
        assert_eq!(decoded.elements.len(), count, "{expr}");
    }
    schema.drop().await
}

#[tokio::test]
async fn text_array_literals_are_read_back_by_the_server() -> TestResult {
    let (schema, client) = connect!();
    // quotes, backslashes, separators, braces, the word NULL, blanks and non-ASCII must all survive
    let element = prop_oneof![
        3 => "[^\\x00]{0,10}",
        1 => Just("NULL".to_owned()),
        1 => Just("\"\\{},".to_owned()),
        1 => Just(" padded ".to_owned()),
        1 => Just(String::new()),
    ];
    for (dims, elements) in samples(shaped(element), CASES) {
        let texts: Vec<Option<Vec<u8>>> = elements
            .iter()
            .map(|e| e.as_ref().map(|s| s.clone().into_bytes()))
            .collect();
        let literal = array::encode_literal(&dims, &borrowed(&texts));
        if dims.is_empty() || dims.contains(&0) {
            continue;
        }
        let sent = client
            .query_one("SELECT array_send($1::text::text[])", &[&literal])
            .await?;
        let raw: Vec<u8> = sent.get(0);
        let decoded = array::decode(&raw)?;
        assert_eq!(decoded.element_oid, 25, "{literal}");
        assert_eq!(decoded.dims, dims, "{literal}");
        assert_eq!(decoded.elements, borrowed(&texts), "{literal}");
        // the same literal sent as a text-format parameter, as rowx does when an element is text
        let shown = read_back(&client, "text[]", Param::text(literal.clone().into_bytes())).await?;
        let canonical = literal_text(&client, "text[]", &literal).await?;
        assert_eq!(shown, canonical, "{literal}");
    }
    schema.drop().await
}

fn ipv4_text(addr: [u8; 4], bits: u8) -> String {
    let [a, b, c, d] = addr;
    format!("{a}.{b}.{c}.{d}/{bits}")
}

fn ipv6_text(addr: [u8; 16], bits: u8) -> String {
    let groups: Vec<String> = addr
        .chunks(2)
        .map(|pair| format!("{:x}", u16::from_be_bytes([pair[0], pair[1]])))
        .collect();
    format!("{}/{bits}", groups.join(":"))
}

// clears every bit past the prefix, which is what a cidr value requires
fn masked_v4(addr: [u8; 4], bits: u8) -> [u8; 4] {
    let mask = u32::MAX.checked_shl(32 - u32::from(bits)).unwrap_or(0);
    (u32::from_be_bytes(addr) & mask).to_be_bytes()
}

fn masked_v6(addr: [u8; 16], bits: u8) -> [u8; 16] {
    let mask = u128::MAX.checked_shl(128 - u32::from(bits)).unwrap_or(0);
    (u128::from_be_bytes(addr) & mask).to_be_bytes()
}

fn networks() -> impl Strategy<Value = Network> {
    prop_oneof![
        (any::<[u8; 4]>(), 0u8..=32).prop_map(|(addr, bits)| Network {
            addr: Addr::V4(addr),
            bits,
            is_cidr: false
        }),
        (any::<[u8; 16]>(), 0u8..=128).prop_map(|(addr, bits)| Network {
            addr: Addr::V6(addr),
            bits,
            is_cidr: false
        }),
    ]
}

fn text_of(network: &Network) -> String {
    match network.addr {
        Addr::V4(addr) => ipv4_text(addr, network.bits),
        Addr::V6(addr) => ipv6_text(addr, network.bits),
    }
}

#[tokio::test]
async fn inet_and_cidr_match_the_server_send_functions() -> TestResult {
    let (schema, client) = connect!();
    for network in samples(networks(), CASES) {
        let text = text_of(&network);
        let sent = send(&client, "inet_send", "inet", &text).await?;
        assert_eq!(network::encode(&network), Ok(sent.clone()), "inet {text}");
        assert_eq!(network::decode(&sent), Ok(network), "inet {text}");

        let masked = Network {
            addr: match network.addr {
                Addr::V4(addr) => Addr::V4(masked_v4(addr, network.bits)),
                Addr::V6(addr) => Addr::V6(masked_v6(addr, network.bits)),
            },
            is_cidr: true,
            ..network
        };
        let text = text_of(&masked);
        let sent = send(&client, "cidr_send", "cidr", &text).await?;
        assert_eq!(network::encode(&masked), Ok(sent.clone()), "cidr {text}");
        assert_eq!(network::decode(&sent), Ok(masked), "cidr {text}");
    }
    schema.drop().await
}

#[tokio::test]
async fn macaddr_and_macaddr8_decode_what_the_server_sends() -> TestResult {
    let (schema, client) = connect!();
    for raw in samples(any::<[u8; MAC_LEN]>(), CASES) {
        let text = mac::decode(&raw, MAC_LEN)?;
        // the server's own text rendering is the oracle for the text rowx builds
        assert_eq!(literal_text(&client, "macaddr", &text).await?, text);
        assert_eq!(
            send(&client, "macaddr_send", "macaddr", &text).await?,
            raw.to_vec()
        );
    }
    for raw in samples(any::<[u8; MAC8_LEN]>(), CASES) {
        let text = mac::decode(&raw, MAC8_LEN)?;
        assert_eq!(literal_text(&client, "macaddr8", &text).await?, text);
        assert_eq!(
            send(&client, "macaddr8_send", "macaddr8", &text).await?,
            raw.to_vec()
        );
    }
    schema.drop().await
}
