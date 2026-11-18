// stored values are untrusted: no panicking index, slice or arithmetic (this comment also keeps `#![` off line 1 for the prek shebang check, so do not delete it)
#![deny(clippy::indexing_slicing, clippy::arithmetic_side_effects)]

pub mod blob;
pub mod boolean;
pub mod decimal;
pub mod integer;
pub mod json;
pub mod real;
pub mod temporal;
pub mod text;
pub mod uuid;

use rusqlite::types::{Value, ValueRef};
use thiserror::Error;

use crate::types::Kind;
use decimal::Decimal;
use temporal::{Aware, Date, DateTime, Time};
use uuid::Uuid;

#[derive(Debug, Clone, PartialEq, Eq, Error)]
#[error("{0}")]
pub struct DecodeError(Box<str>);

impl DecodeError {
    #[must_use]
    pub fn new(message: impl Into<Box<str>>) -> Self {
        Self(message.into())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Error)]
#[error("{0}")]
pub struct EncodeError(Box<str>);

impl EncodeError {
    #[must_use]
    pub fn new(message: impl Into<Box<str>>) -> Self {
        Self(message.into())
    }
}

pub(crate) const fn class_name(value: ValueRef<'_>) -> &'static str {
    match value {
        ValueRef::Null => "NULL",
        ValueRef::Integer(_) => "INTEGER",
        ValueRef::Real(_) => "REAL",
        ValueRef::Text(_) => "TEXT",
        ValueRef::Blob(_) => "BLOB",
    }
}

// a stored value whose storage class disagrees with the declared type is never coerced
pub(crate) fn mismatch(expected: &str, found: ValueRef<'_>) -> DecodeError {
    DecodeError::new(format!(
        "expected {expected} but the stored value is {}",
        class_name(found)
    ))
}

/// A stored integer as an `f64`, when that is exact (magnitude up to 2^53).
pub(crate) fn exact_f64(value: i64) -> Option<f64> {
    if value.unsigned_abs() > 1 << 53 {
        return None;
    }
    // split into 32-bit halves so every step is an exact `From` conversion
    let high = i32::try_from(value.div_euclid(1 << 32)).ok()?;
    let low = u32::try_from(value.rem_euclid(1 << 32)).ok()?;
    Some(f64::from(high).mul_add(4_294_967_296.0, f64::from(low)))
}

/// The integer part of a float inside the `i64` range.
// the range check above the cast makes the truncation lossless; no safe float-to-integer conversion exists
#[allow(clippy::cast_possible_truncation)]
pub(crate) fn trunc_i64(value: f64) -> Option<i64> {
    // 2^62 is exactly representable and leaves headroom for the callers' checked arithmetic
    if value.is_finite() && value.abs() < 4_611_686_018_427_387_904.0 {
        Some(value.trunc() as i64)
    } else {
        None
    }
}

/// A non-NULL value in the pure representation the binding layer converts to Python.
#[derive(Debug, Clone, PartialEq)]
pub enum Scalar {
    Integer(i64),
    Real(f64),
    Text(String),
    Blob(Vec<u8>),
    Boolean(bool),
    Date(Date),
    Time(Time),
    /// Naive.
    DateTime(DateTime),
    /// Aware; a decoded value is always UTC (`offset_seconds` 0).
    Aware(Aware),
    Decimal(Decimal),
    Uuid(Uuid),
    Json(String),
}

/// Decodes a stored value for a column of `kind`; NULL is `None`.
///
/// # Errors
/// A [`DecodeError`] when the stored value does not fit the kind.
pub fn decode(kind: Kind, value: ValueRef<'_>) -> Result<Option<Scalar>, DecodeError> {
    if matches!(value, ValueRef::Null) {
        return Ok(None);
    }
    let scalar = match kind {
        Kind::Integer => Scalar::Integer(integer::decode(value)?),
        Kind::Real => Scalar::Real(real::decode(value)?),
        Kind::Text => Scalar::Text(text::decode(value)?),
        Kind::Blob => Scalar::Blob(blob::decode(value)?),
        Kind::Boolean => Scalar::Boolean(boolean::decode(value)?),
        Kind::DateTime => Scalar::DateTime(temporal::decode_datetime(value)?),
        Kind::DateTimeTz => Scalar::Aware(temporal::decode_aware(value)?),
        Kind::Date => Scalar::Date(temporal::decode_date(value)?),
        Kind::Time => Scalar::Time(temporal::decode_time(value)?),
        Kind::Decimal => Scalar::Decimal(decimal::decode(value)?),
        Kind::Uuid => Scalar::Uuid(uuid::decode(value)?),
        Kind::Json => Scalar::Json(json::decode(value)?),
        Kind::Any => decode_any(value)?,
    };
    Ok(Some(scalar))
}

fn decode_any(value: ValueRef<'_>) -> Result<Scalar, DecodeError> {
    Ok(match value {
        ValueRef::Integer(int) => Scalar::Integer(int),
        ValueRef::Real(float) => Scalar::Real(float),
        ValueRef::Text(_) => Scalar::Text(text::decode(value)?),
        ValueRef::Blob(bytes) => Scalar::Blob(bytes.to_vec()),
        ValueRef::Null => return Err(mismatch("a value", value)),
    })
}

/// Encodes a value for a column of `kind`.
///
/// # Errors
/// An [`EncodeError`] when the value is not the column's kind (a naive datetime for an aware column,
/// or the reverse, included) or cannot be stored.
pub fn encode(kind: Kind, scalar: &Scalar) -> Result<Value, EncodeError> {
    match (kind, scalar) {
        (Kind::Integer | Kind::Any, Scalar::Integer(int)) => Ok(integer::encode(*int)),
        (Kind::Real | Kind::Any, Scalar::Real(float)) => real::encode(*float),
        (Kind::Text | Kind::Any, Scalar::Text(string)) => text::encode(string),
        (Kind::Blob | Kind::Any, Scalar::Blob(bytes)) => Ok(blob::encode(bytes.clone())),
        (Kind::Boolean, Scalar::Boolean(flag)) => Ok(boolean::encode(*flag)),
        (Kind::DateTime, Scalar::DateTime(datetime)) => temporal::encode_datetime(datetime),
        (Kind::DateTimeTz, Scalar::Aware(aware)) => temporal::encode_aware(aware),
        (Kind::Date, Scalar::Date(date)) => temporal::encode_date(*date),
        (Kind::Time, Scalar::Time(time)) => temporal::encode_time(*time),
        (Kind::Decimal, Scalar::Decimal(decimal)) => Ok(decimal::encode(decimal)),
        (Kind::Uuid, Scalar::Uuid(uuid)) => Ok(uuid::encode(uuid)),
        (Kind::Json, Scalar::Json(json)) => json::encode(json),
        (Kind::DateTime, Scalar::Aware(_)) => Err(EncodeError::new(
            "an aware datetime cannot be stored in a naive datetime column; use a TIMESTAMPTZ column or drop the zone",
        )),
        (Kind::DateTimeTz, Scalar::DateTime(_)) => Err(EncodeError::new(
            "a naive datetime cannot be stored in a TIMESTAMPTZ column; attach a UTC offset",
        )),
        (kind, scalar) => Err(EncodeError::new(format!(
            "a {} value does not fit a {kind:?} column",
            scalar_name(scalar)
        ))),
    }
}

const fn scalar_name(scalar: &Scalar) -> &'static str {
    match scalar {
        Scalar::Integer(_) => "integer",
        Scalar::Real(_) => "real",
        Scalar::Text(_) => "text",
        Scalar::Blob(_) => "blob",
        Scalar::Boolean(_) => "boolean",
        Scalar::Date(_) => "date",
        Scalar::Time(_) => "time",
        Scalar::DateTime(_) => "datetime",
        Scalar::Aware(_) => "aware datetime",
        Scalar::Decimal(_) => "decimal",
        Scalar::Uuid(_) => "uuid",
        Scalar::Json(_) => "json",
    }
}

#[cfg(test)]
mod tests {
    use super::temporal::{Aware, Date, DateTime, Time};
    use super::{Scalar, decode, encode, exact_f64, trunc_i64};
    use crate::types::Kind;
    use rusqlite::types::{Value, ValueRef};

    fn datetime() -> DateTime {
        DateTime {
            date: Date {
                year: 2024,
                month: 5,
                day: 6,
            },
            time: Time {
                hour: 7,
                minute: 8,
                second: 9,
                microsecond: 0,
            },
        }
    }

    #[test]
    fn null_decodes_to_none_for_every_kind() {
        for kind in [Kind::Integer, Kind::Boolean, Kind::DateTimeTz, Kind::Any] {
            assert_eq!(decode(kind, ValueRef::Null), Ok(None));
        }
    }

    #[test]
    fn any_decodes_by_storage_class() {
        assert_eq!(
            decode(Kind::Any, ValueRef::Integer(5)),
            Ok(Some(Scalar::Integer(5)))
        );
        assert_eq!(
            decode(Kind::Any, ValueRef::Real(1.5)),
            Ok(Some(Scalar::Real(1.5)))
        );
        assert_eq!(
            decode(Kind::Any, ValueRef::Text(b"hi")),
            Ok(Some(Scalar::Text("hi".to_owned())))
        );
        assert_eq!(
            decode(Kind::Any, ValueRef::Blob(&[1, 2])),
            Ok(Some(Scalar::Blob(vec![1, 2])))
        );
        assert!(decode(Kind::Any, ValueRef::Text(&[0xff])).is_err());
    }

    #[test]
    fn encode_dispatches_by_kind() {
        assert_eq!(
            encode(Kind::Integer, &Scalar::Integer(3)),
            Ok(Value::Integer(3))
        );
        assert_eq!(
            encode(Kind::Any, &Scalar::Text("x".to_owned())),
            Ok(Value::Text("x".to_owned()))
        );
        assert_eq!(
            encode(Kind::Boolean, &Scalar::Boolean(true)),
            Ok(Value::Integer(1))
        );
        assert_eq!(
            encode(Kind::DateTime, &Scalar::DateTime(datetime())),
            Ok(Value::Text("2024-05-06 07:08:09".to_owned()))
        );
    }

    #[test]
    fn naive_and_aware_datetimes_do_not_cross_columns() {
        let aware = Scalar::Aware(Aware {
            local: datetime(),
            offset_seconds: 0,
        });
        assert!(encode(Kind::DateTime, &aware).is_err());
        assert!(encode(Kind::DateTimeTz, &Scalar::DateTime(datetime())).is_err());
        assert!(encode(Kind::DateTimeTz, &aware).is_ok());
    }

    #[test]
    fn a_value_of_the_wrong_kind_is_an_encode_error() {
        assert!(encode(Kind::Integer, &Scalar::Text("1".to_owned())).is_err());
        assert!(encode(Kind::Text, &Scalar::Integer(1)).is_err());
        assert!(encode(Kind::Real, &Scalar::Integer(1)).is_err());
    }

    #[test]
    fn exact_f64_stops_at_two_to_the_53() {
        assert_eq!(exact_f64(0), Some(0.0));
        assert_eq!(exact_f64(-1), Some(-1.0));
        assert_eq!(
            exact_f64(9_007_199_254_740_992),
            Some(9_007_199_254_740_992.0)
        );
        assert_eq!(
            exact_f64(-9_007_199_254_740_992),
            Some(-9_007_199_254_740_992.0)
        );
        assert_eq!(exact_f64(9_007_199_254_740_993), None);
        assert_eq!(exact_f64(i64::MIN), None);
        assert_eq!(exact_f64(4_294_967_297), Some(4_294_967_297.0));
    }

    #[test]
    fn trunc_i64_rejects_non_finite_and_huge_values() {
        assert_eq!(trunc_i64(1.9), Some(1));
        assert_eq!(trunc_i64(-1.9), Some(-1));
        assert_eq!(trunc_i64(f64::NAN), None);
        assert_eq!(trunc_i64(f64::INFINITY), None);
        assert_eq!(trunc_i64(1e19), None);
    }
}
