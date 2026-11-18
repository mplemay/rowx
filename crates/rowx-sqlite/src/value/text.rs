use rusqlite::types::{Value, ValueRef};

use super::{DecodeError, EncodeError, mismatch};

/// # Errors
/// Any storage class but TEXT, and TEXT that is not valid UTF-8.
pub fn decode_str(value: ValueRef<'_>) -> Result<&str, DecodeError> {
    match value {
        ValueRef::Text(bytes) => std::str::from_utf8(bytes)
            .map_err(|err| DecodeError::new(format!("the stored TEXT is not valid UTF-8: {err}"))),
        other => Err(mismatch("TEXT", other)),
    }
}

/// # Errors
/// As [`decode_str`].
pub fn decode(value: ValueRef<'_>) -> Result<String, DecodeError> {
    decode_str(value).map(str::to_owned)
}

/// # Errors
/// An embedded NUL, which SQLite would silently cut the text at in several APIs.
pub fn encode(value: &str) -> Result<Value, EncodeError> {
    if value.contains('\0') {
        return Err(EncodeError::new(
            "text containing a NUL character cannot be stored",
        ));
    }
    Ok(Value::Text(value.to_owned()))
}

#[cfg(test)]
mod tests {
    use super::{decode, decode_str, encode};
    use rusqlite::types::{Value, ValueRef};

    #[test]
    fn text_round_trips() {
        assert_eq!(
            decode(ValueRef::Text("héllo".as_bytes())),
            Ok("héllo".to_owned())
        );
        assert_eq!(decode_str(ValueRef::Text(b"")), Ok(""));
        assert_eq!(encode("héllo"), Ok(Value::Text("héllo".to_owned())));
    }

    #[test]
    fn invalid_utf8_raises() {
        assert!(decode(ValueRef::Text(&[0xff, 0xfe])).is_err());
        assert!(decode(ValueRef::Text(&[b'a', 0xc3])).is_err());
    }

    #[test]
    fn embedded_nul_is_rejected_on_encode() {
        assert!(encode("a\0b").is_err());
        assert!(encode("").is_ok());
    }

    #[test]
    fn other_storage_classes_are_never_coerced() {
        for value in [
            ValueRef::Integer(1),
            ValueRef::Real(1.0),
            ValueRef::Blob(b"x"),
        ] {
            assert!(decode(value).is_err(), "{value:?}");
        }
    }
}
