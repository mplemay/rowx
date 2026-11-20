use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyDict, PyType};
use rowx_sqlite::exec::Param;
use rowx_sqlite::value::temporal::{self, Aware, Date, DateTime, Time};

use super::classes::class;
use rowx_py_core::errors::encode_error;

static DATE: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static TIME: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static DATETIME: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static TIMEZONE: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static UTC: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

const SECONDS_PER_DAY: i64 = 86_400;

/// # Errors
/// An import failure of the `datetime` module.
pub fn date_class(py: Python<'_>) -> PyResult<&Bound<'_, PyType>> {
    class(py, &DATE, "datetime", "date")
}

/// # Errors
/// An import failure of the `datetime` module.
pub fn time_class(py: Python<'_>) -> PyResult<&Bound<'_, PyType>> {
    class(py, &TIME, "datetime", "time")
}

/// # Errors
/// An import failure of the `datetime` module.
pub fn datetime_class(py: Python<'_>) -> PyResult<&Bound<'_, PyType>> {
    class(py, &DATETIME, "datetime", "datetime")
}

fn utc(py: Python<'_>) -> PyResult<&Bound<'_, PyAny>> {
    let tz = UTC.get_or_try_init(py, || {
        Ok::<_, PyErr>(
            class(py, &TIMEZONE, "datetime", "timezone")?
                .getattr("utc")?
                .unbind(),
        )
    })?;
    Ok(tz.bind(py))
}

/// # Errors
/// An error from `datetime.date`.
pub fn decode_date(py: Python<'_>, value: Date) -> PyResult<Bound<'_, PyAny>> {
    date_class(py)?.call1((value.year, value.month, value.day))
}

/// # Errors
/// An error from `datetime.time`.
pub fn decode_time(py: Python<'_>, value: Time) -> PyResult<Bound<'_, PyAny>> {
    time_class(py)?.call1((value.hour, value.minute, value.second, value.microsecond))
}

const fn datetime_args(value: DateTime) -> (u16, u8, u8, u8, u8, u8, u32) {
    (
        value.date.year,
        value.date.month,
        value.date.day,
        value.time.hour,
        value.time.minute,
        value.time.second,
        value.time.microsecond,
    )
}

/// # Errors
/// An error from `datetime.datetime`.
pub fn decode_datetime(py: Python<'_>, value: DateTime) -> PyResult<Bound<'_, PyAny>> {
    datetime_class(py)?.call1(datetime_args(value))
}

/// A stored aware datetime is always UTC.
///
/// # Errors
/// An error from `datetime.datetime`.
pub fn decode_aware(py: Python<'_>, value: Aware) -> PyResult<Bound<'_, PyAny>> {
    let kwargs = PyDict::new(py);
    kwargs.set_item("tzinfo", utc(py)?)?;
    datetime_class(py)?.call(datetime_args(value.local), Some(&kwargs))
}

fn small(obj: &Bound<'_, PyAny>, name: &str) -> PyResult<u8> {
    obj.getattr(name)?.extract::<u8>()
}

fn year(obj: &Bound<'_, PyAny>) -> PyResult<u16> {
    obj.getattr("year")?.extract::<u16>()
}

fn big(obj: &Bound<'_, PyAny>, name: &str) -> PyResult<i64> {
    obj.getattr(name)?.extract::<i64>()
}

fn date_of(obj: &Bound<'_, PyAny>) -> PyResult<Date> {
    Ok(Date {
        year: year(obj)?,
        month: small(obj, "month")?,
        day: small(obj, "day")?,
    })
}

fn time_of(obj: &Bound<'_, PyAny>) -> PyResult<Time> {
    Ok(Time {
        hour: small(obj, "hour")?,
        minute: small(obj, "minute")?,
        second: small(obj, "second")?,
        microsecond: obj.getattr("microsecond")?.extract::<u32>()?,
    })
}

// the UTC offset in whole seconds, or `None` for a naive value
fn offset_of(py: Python<'_>, obj: &Bound<'_, PyAny>) -> PyResult<Option<i32>> {
    let delta = obj.call_method0("utcoffset")?;
    if delta.is_none() {
        return Ok(None);
    }
    let days = big(&delta, "days")?;
    let seconds = big(&delta, "seconds")?;
    let micros = big(&delta, "microseconds")?;
    if micros != 0 {
        return Err(encode_error(
            py,
            "a UTC offset with a fraction of a second cannot be stored",
        ));
    }
    days.checked_mul(SECONDS_PER_DAY)
        .and_then(|total| total.checked_add(seconds))
        .and_then(|total| i32::try_from(total).ok())
        .map(Some)
        .ok_or_else(|| encode_error(py, "the UTC offset is out of range"))
}

/// A `datetime.date` that is not a `datetime`.
///
/// # Errors
/// `EncodeError` for a date SQLite cannot store (the year must be 1 to 9999).
pub fn encode_date(py: Python<'_>, obj: &Bound<'_, PyAny>) -> PyResult<Param> {
    temporal::encode_date(date_of(obj)?).map_err(|e| encode_error(py, &e.to_string()))
}

/// # Errors
/// `EncodeError` for an aware time, which has no stored form.
pub fn encode_time(py: Python<'_>, obj: &Bound<'_, PyAny>) -> PyResult<Param> {
    if offset_of(py, obj)?.is_some() {
        return Err(encode_error(
            py,
            "an aware time cannot be stored; drop the zone",
        ));
    }
    temporal::encode_time(time_of(obj)?).map_err(|e| encode_error(py, &e.to_string()))
}

/// A naive datetime is written as it is and an aware one as UTC.
///
/// # Errors
/// `EncodeError` for a value SQLite cannot store.
pub fn encode_datetime(py: Python<'_>, obj: &Bound<'_, PyAny>) -> PyResult<Param> {
    let local = DateTime {
        date: date_of(obj)?,
        time: time_of(obj)?,
    };
    let stored = offset_of(py, obj)?.map_or_else(
        || temporal::encode_datetime(local),
        |offset_seconds| {
            temporal::encode_aware(Aware {
                local,
                offset_seconds,
            })
        },
    );
    stored.map_err(|e| encode_error(py, &e.to_string()))
}
