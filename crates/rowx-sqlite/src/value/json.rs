use rusqlite::types::{Value, ValueRef};

use super::{DecodeError, EncodeError, mismatch, text};

// JSON syntax is not validated here (that would need a parser dependency); the binding layer parses the
// text and reports a syntax error. A BLOB is SQLite's binary JSONB, which rowx does not read.

/// # Errors
/// Anything but TEXT (a BLOB is JSONB), and TEXT that is not valid UTF-8.
pub fn decode(value: ValueRef<'_>) -> Result<String, DecodeError> {
    match value {
        ValueRef::Blob(_) => Err(DecodeError::new(
            "a JSON column holds a BLOB (JSONB), which is not supported; store JSON as TEXT",
        )),
        ValueRef::Text(_) => text::decode(value),
        other => Err(mismatch("TEXT for a JSON column", other)),
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
    fn jsonb_blob_and_other_classes_raise() {
        for value in [
            ValueRef::Blob(&[0x7c, 0x17]),
            ValueRef::Integer(5),
            ValueRef::Real(1.5),
            ValueRef::Null,
            ValueRef::Text(&[0xff]),
        ] {
            assert!(decode(value).is_err(), "{value:?}");
        }
    }

    #[test]
    fn embedded_nul_is_rejected_on_encode() {
        assert!(encode("\"a\0\"").is_err());
    }
}
