use rusqlite::types::{Value, ValueRef};

use super::{DecodeError, mismatch};

/// # Errors
/// Anything but the INTEGER 0 or 1.
pub fn decode(value: ValueRef<'_>) -> Result<bool, DecodeError> {
    match value {
        ValueRef::Integer(0) => Ok(false),
        ValueRef::Integer(1) => Ok(true),
        ValueRef::Integer(other) => Err(DecodeError::new(format!(
            "expected the INTEGER 0 or 1 for a BOOLEAN but the stored value is {other}"
        ))),
        other => Err(mismatch("INTEGER 0 or 1 for a BOOLEAN", other)),
    }
}

#[must_use]
pub fn encode(value: bool) -> Value {
    Value::Integer(i64::from(value))
}

#[cfg(test)]
mod tests {
    use super::{decode, encode};
    use rusqlite::types::{Value, ValueRef};

    #[test]
    fn zero_and_one_are_booleans() {
        assert_eq!(decode(ValueRef::Integer(0)), Ok(false));
        assert_eq!(decode(ValueRef::Integer(1)), Ok(true));
        assert_eq!(encode(false), Value::Integer(0));
        assert_eq!(encode(true), Value::Integer(1));
    }

    #[test]
    fn anything_else_raises() {
        for value in [
            ValueRef::Integer(2),
            ValueRef::Integer(-1),
            ValueRef::Real(1.0),
            ValueRef::Text(b"true"),
            ValueRef::Text(b"1"),
            ValueRef::Blob(&[1]),
            ValueRef::Null,
        ] {
            assert!(decode(value).is_err(), "{value:?}");
        }
    }
}
