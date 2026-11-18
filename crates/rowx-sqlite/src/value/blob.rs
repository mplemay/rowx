use rusqlite::types::{Value, ValueRef};

use super::{DecodeError, mismatch};

/// # Errors
/// Any storage class but BLOB.
pub fn decode_slice(value: ValueRef<'_>) -> Result<&[u8], DecodeError> {
    match value {
        ValueRef::Blob(bytes) => Ok(bytes),
        other => Err(mismatch("BLOB", other)),
    }
}

/// # Errors
/// As [`decode_slice`].
pub fn decode(value: ValueRef<'_>) -> Result<Vec<u8>, DecodeError> {
    decode_slice(value).map(<[u8]>::to_vec)
}

#[must_use]
pub const fn encode(value: Vec<u8>) -> Value {
    Value::Blob(value)
}

#[cfg(test)]
mod tests {
    use super::{decode, decode_slice, encode};
    use rusqlite::types::{Value, ValueRef};

    #[test]
    fn blobs_round_trip() {
        assert_eq!(decode(ValueRef::Blob(&[0, 1, 255])), Ok(vec![0, 1, 255]));
        assert_eq!(decode_slice(ValueRef::Blob(&[])), Ok(&[][..]));
        assert_eq!(encode(vec![0, 1]), Value::Blob(vec![0, 1]));
    }

    #[test]
    fn text_is_never_coerced_to_bytes() {
        for value in [
            ValueRef::Text(b"ab"),
            ValueRef::Integer(1),
            ValueRef::Real(1.0),
        ] {
            assert!(decode(value).is_err(), "{value:?}");
        }
    }
}
