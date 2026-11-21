use rusqlite::types::{Value, ValueRef};

use super::{DecodeError, EncodeError, exact_f64, mismatch};

/// An INTEGER is accepted when it converts exactly (magnitude up to 2^53), because REAL affinity
/// stores such values as integers; infinities pass through.
///
/// # Errors
/// TEXT or BLOB, and an INTEGER a float cannot hold exactly.
pub fn decode(value: ValueRef<'_>) -> Result<f64, DecodeError> {
    match value {
        ValueRef::Real(float) => Ok(float),
        ValueRef::Integer(int) => exact_f64(int).ok_or_else(|| {
            DecodeError::new(format!(
                "the stored INTEGER {int} cannot be read as a float without losing precision"
            ))
        }),
        other => Err(mismatch("REAL", other)),
    }
}

/// # Errors
/// NaN, which SQLite would store as NULL. Infinities are stored as they are.
pub fn encode(value: f64) -> Result<Value, EncodeError> {
    if value.is_nan() {
        return Err(EncodeError::new(
            "NaN cannot be stored in a REAL column (SQLite would store NULL)",
        ));
    }
    Ok(Value::Real(value))
}

#[cfg(test)]
mod tests {
    use super::{decode, encode};
    use rusqlite::types::{Value, ValueRef};

    #[test]
    fn reals_round_trip_including_infinities() {
        for value in [0.0, -0.5, 1.25e300, f64::INFINITY, f64::NEG_INFINITY] {
            assert_eq!(decode(ValueRef::Real(value)), Ok(value));
            assert_eq!(encode(value), Ok(Value::Real(value)));
        }
    }

    #[test]
    fn nan_is_rejected_on_encode() {
        assert!(encode(f64::NAN).is_err());
    }

    #[test]
    fn exact_integers_are_accepted_and_lossy_ones_are_errors() {
        assert_eq!(decode(ValueRef::Integer(3)), Ok(3.0));
        assert_eq!(decode(ValueRef::Integer(-7)), Ok(-7.0));
        assert_eq!(
            decode(ValueRef::Integer(9_007_199_254_740_992)),
            Ok(9_007_199_254_740_992.0)
        );
        assert!(decode(ValueRef::Integer(9_007_199_254_740_993)).is_err());
        assert!(decode(ValueRef::Integer(i64::MAX)).is_err());
    }

    #[test]
    fn text_and_blob_are_never_coerced() {
        assert!(decode(ValueRef::Text(b"1.5")).is_err());
        assert!(decode(ValueRef::Blob(&[1])).is_err());
        assert!(decode(ValueRef::Null).is_err());
    }
}
