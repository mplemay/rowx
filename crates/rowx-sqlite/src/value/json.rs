use rusqlite::types::{Value, ValueRef};

use super::{DecodeError, EncodeError, mismatch, text};

// JSON syntax is not validated here (that would need a parser dependency); the binding layer parses the
// text and reports a syntax error. A BLOB is SQLite's binary JSONB, which rowx does not read.

/// Reads TEXT as is.
///
/// A column declared `JSON` has NUMERIC affinity, so SQLite stores the text `5` as the INTEGER 5 and
/// `1e3` as the INTEGER 1000; those read back as their JSON number text, an INTEGER in decimal and a
/// finite REAL in Rust's shortest round-trip `{:?}` form.
///
/// # Errors
/// A BLOB (JSONB), a non-finite REAL, NULL, and TEXT that is not valid UTF-8.
pub fn decode(value: ValueRef<'_>) -> Result<String, DecodeError> {
    match value {
        ValueRef::Integer(int) => Ok(int.to_string()),
        ValueRef::Real(float) if float.is_finite() => Ok(format!("{float:?}")),
        ValueRef::Real(_) => Err(DecodeError::new(
            "a JSON column holds a non-finite REAL, which is not a JSON number",
        )),
        ValueRef::Blob(_) => Err(DecodeError::new(
            "a JSON column holds a BLOB (JSONB), which is not supported; store JSON as TEXT",
        )),
        ValueRef::Text(_) => text::decode(value),
        other @ ValueRef::Null => Err(mismatch("TEXT for a JSON column", other)),
    }
}

/// # Errors
/// An embedded NUL.
pub fn encode(value: &str) -> Result<Value, EncodeError> {
    text::encode(value)
}

#[cfg(test)]
mod tests {
    use super::{decode, encode};
    use rusqlite::types::{Value, ValueRef};

    #[test]
    fn json_text_round_trips_unparsed() {
        assert_eq!(
            decode(ValueRef::Text(br#"{"a": [1, 2]}"#)),
            Ok(r#"{"a": [1, 2]}"#.to_owned())
        );
        assert_eq!(encode("[1]"), Ok(Value::Text("[1]".to_owned())));
        // not validated at this layer
        assert_eq!(
            decode(ValueRef::Text(b"not json")),
            Ok("not json".to_owned())
        );
    }

    #[test]
    fn jsonb_blob_non_finite_reals_and_null_raise() {
        for value in [
            ValueRef::Blob(&[0x7c, 0x17]),
            ValueRef::Real(f64::NAN),
            ValueRef::Real(f64::INFINITY),
            ValueRef::Null,
            ValueRef::Text(&[0xff]),
        ] {
            assert!(decode(value).is_err(), "{value:?}");
        }
    }

    #[test]
    fn numeric_affinity_scalars_read_as_json_numbers() {
        assert_eq!(decode(ValueRef::Integer(5)), Ok("5".to_owned()));
        assert_eq!(decode(ValueRef::Real(1.5)), Ok("1.5".to_owned()));
        assert_eq!(decode(ValueRef::Real(1e21)), Ok("1e21".to_owned()));
    }

    #[test]
    fn json_column_round_trips_through_sqlite() {
        let conn = rusqlite::Connection::open_in_memory();
        assert!(conn.is_ok());
        if let Ok(conn) = conn {
            assert!(conn.execute_batch("CREATE TABLE t(j JSON)").is_ok());
            for text in ["5", "1.5", "[1]"] {
                let stored = encode(text);
                assert!(stored.is_ok(), "{text}");
                if let Ok(stored) = stored {
                    assert!(
                        conn.execute("DELETE FROM t", []).is_ok()
                            && conn.execute("INSERT INTO t VALUES (?1)", [&stored]).is_ok()
                    );
                }
                let read = conn.query_row("SELECT j FROM t", [], |row| {
                    let value = row.get_ref(0)?;
                    Ok(crate::value::decode(crate::types::Kind::Json, value))
                });
                assert_eq!(
                    read,
                    Ok(Ok(Some(crate::value::Scalar::Json(text.to_owned())))),
                    "{text}"
                );
            }
        }
    }

    #[test]
    fn embedded_nul_is_rejected_on_encode() {
        assert!(encode("\"a\0\"").is_err());
    }
}
