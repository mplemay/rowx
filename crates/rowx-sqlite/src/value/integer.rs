use rusqlite::types::{Value, ValueRef};

use super::{DecodeError, mismatch};

/// # Errors
/// Any storage class but INTEGER (TEXT in an INTEGER column is never parsed).
pub fn decode(value: ValueRef<'_>) -> Result<i64, DecodeError> {
    match value {
        ValueRef::Integer(int) => Ok(int),
        other => Err(mismatch("INTEGER", other)),
    }
}

#[must_use]
pub const fn encode(value: i64) -> Value {
    Value::Integer(value)
}

#[cfg(test)]
mod tests {
    use super::{decode, encode};
    use rusqlite::types::{Value, ValueRef};

    #[test]
    fn integers_round_trip() {
        for value in [0, 1, -1, i64::MAX, i64::MIN] {
            assert_eq!(decode(ValueRef::Integer(value)), Ok(value));
            assert_eq!(encode(value), Value::Integer(value));
        }
    }

    #[test]
    fn other_storage_classes_are_never_coerced() {
        for value in [
            ValueRef::Text(b"42"),
            ValueRef::Real(42.0),
            ValueRef::Blob(&[42]),
            ValueRef::Null,
        ] {
            assert!(decode(value).is_err(), "{value:?}");
        }
        assert_eq!(
            decode(ValueRef::Text(b"42")).map_err(|err| err.to_string()),
            Err("expected INTEGER but the stored value is TEXT".to_owned())
        );
    }
}
