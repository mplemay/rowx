use rowx_postgres::wire::temporal::{self, TemporalError};

use crate::codec::extract::PyParam;

#[derive(Clone, Copy, Debug)]
pub struct Ymd {
    pub year: i32,
    pub month: u32,
    pub day: u32,
}

const MICROS_PER_SECOND: i64 = 1_000_000;

fn mismatch(param: &PyParam, ty: &str) -> String {
    format!("a Python {} cannot be encoded as {ty}", param.type_name())
}

fn failed(error: &TemporalError) -> String {
    error.to_string()
}

/// # Errors
/// A description of why the value cannot be encoded as `date`.
pub fn encode_date(param: &PyParam) -> Result<Vec<u8>, String> {
    match param {
        PyParam::Date(date) => {
            temporal::encode_date(date.year, date.month, date.day).map_err(|e| failed(&e))
        }
        _ => Err(mismatch(param, "date")),
    }
}

/// # Errors
/// A description of why the value cannot be encoded as `time`.
pub fn encode_time(param: &PyParam) -> Result<Vec<u8>, String> {
    match param {
        PyParam::Time {
            micros,
            offset: None,
        } => Ok(temporal::encode_time(*micros)),
        PyParam::Time { .. } => {
            Err("a timezone-aware time cannot be encoded as time; pass a naive time".to_owned())
        }
        _ => Err(mismatch(param, "time")),
    }
}

/// # Errors
/// A description of why the value cannot be encoded as `timetz`.
pub fn encode_timetz(param: &PyParam) -> Result<Vec<u8>, String> {
    match param {
        PyParam::Time {
            micros,
            offset: Some(offset),
        } => temporal::encode_timetz_text(*micros, *offset)
            .map(String::into_bytes)
            .map_err(|e| failed(&e)),
        PyParam::Time { .. } => {
            Err("a naive time cannot be encoded as timetz; pass an aware time".to_owned())
        }
        _ => Err(mismatch(param, "timetz")),
    }
}

/// Binary `timetz`, for an array element.
///
/// # Errors
/// A description of why the value cannot be encoded as `timetz`.
pub fn encode_timetz_binary(param: &PyParam) -> Result<Vec<u8>, String> {
    match param {
        PyParam::Time {
            micros,
            offset: Some(offset),
        } => {
            if offset % MICROS_PER_SECOND != 0 {
                return Err(TemporalError::OffsetFraction.to_string());
            }
            // the zone is stored west of UTC, the opposite sign of Python's utcoffset
            let west = (offset / MICROS_PER_SECOND)
                .checked_neg()
                .and_then(|seconds| i32::try_from(seconds).ok())
                .ok_or_else(|| TemporalError::OffsetRange.to_string())?;
            temporal::encode_timetz(*micros, west).map_err(|e| failed(&e))
        }
        PyParam::Time { .. } => {
            Err("a naive time cannot be encoded as timetz; pass an aware time".to_owned())
        }
        _ => Err(mismatch(param, "timetz")),
    }
}

/// # Errors
/// A description of why the value cannot be encoded as `timestamp`.
pub fn encode_timestamp(param: &PyParam) -> Result<Vec<u8>, String> {
    match param {
        PyParam::DateTime {
            date,
            micros,
            offset: None,
        } => temporal::encode_timestamp(date.year, date.month, date.day, *micros, 0)
            .map_err(|e| failed(&e)),
        PyParam::DateTime { .. } => Err(
            "a timezone-aware datetime cannot be encoded as timestamp; pass a naive datetime"
                .to_owned(),
        ),
        _ => Err(mismatch(param, "timestamp")),
    }
}

/// # Errors
/// A description of why the value cannot be encoded as `timestamptz`.
pub fn encode_timestamptz(param: &PyParam) -> Result<Vec<u8>, String> {
    match param {
        PyParam::DateTime {
            date,
            micros,
            offset: Some(offset),
        } => temporal::encode_timestamp(date.year, date.month, date.day, *micros, *offset)
            .map_err(|e| failed(&e)),
        PyParam::DateTime { .. } => Err(
            "a naive datetime cannot be encoded as timestamptz; pass an aware datetime".to_owned(),
        ),
        _ => Err(mismatch(param, "timestamptz")),
    }
}

/// # Errors
/// A description of why the value cannot be encoded as `interval`.
pub fn encode_interval(param: &PyParam) -> Result<Vec<u8>, String> {
    match param {
        PyParam::Timedelta { days, micros } => {
            temporal::encode_interval(*days, *micros).map_err(|e| failed(&e))
        }
        PyParam::Interval {
            months,
            days,
            micros,
        } => temporal::encode_span(*months, *days, *micros).map_err(|e| failed(&e)),
        _ => Err(mismatch(param, "interval")),
    }
}
