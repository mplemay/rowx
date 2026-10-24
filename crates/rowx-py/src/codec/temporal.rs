use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyDict, PyType};
use rowx_pg::Type;
use rowx_pg::wire::temporal::{self, DateTime, TimeOfDay};

use crate::errors::decode_error;

static DATE: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static TIME: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static DATETIME: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static TIMEDELTA: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static TIMEZONE: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static UTC: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

fn class<'py>(
    py: Python<'py>,
    cell: &'static PyOnceLock<Py<PyType>>,
    name: &str,
) -> PyResult<&'py Bound<'py, PyType>> {
    let cls = cell.get_or_try_init(py, || {
        Ok::<_, PyErr>(
            py.import("datetime")?
                .getattr(name)?
                .cast_into::<PyType>()?
                .unbind(),
        )
    })?;
    Ok(cls.bind(py))
}

fn utc(py: Python<'_>) -> PyResult<&Bound<'_, PyAny>> {
    let tz = UTC.get_or_try_init(py, || {
        Ok::<_, PyErr>(class(py, &TIMEZONE, "timezone")?.getattr("utc")?.unbind())
    })?;
    Ok(tz.bind(py))
}

fn fixed_offset(py: Python<'_>, seconds: i32) -> PyResult<Bound<'_, PyAny>> {
    let delta = class(py, &TIMEDELTA, "timedelta")?.call1((0, seconds))?;
    class(py, &TIMEZONE, "timezone")?.call1((delta,))
}

fn build_time<'py>(
    py: Python<'py>,
    t: TimeOfDay,
    tz: Option<Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    let args = (t.hour, t.minute, t.second, t.microsecond);
    match tz {
        Some(tz) => {
            let kwargs = PyDict::new(py);
            kwargs.set_item("tzinfo", tz)?;
            class(py, &TIME, "time")?.call(args, Some(&kwargs))
        }
        None => class(py, &TIME, "time")?.call1(args),
    }
}

/// # Errors
/// `DecodeError` for ±infinity, a year outside 1..9999 or a malformed value.
pub fn decode_date<'py>(py: Python<'py>, raw: &[u8]) -> PyResult<Bound<'py, PyAny>> {
    let d = temporal::decode_date(raw).map_err(|e| decode_error(py, &e.to_string()))?;
    class(py, &DATE, "date")?.call1((d.year, d.month, d.day))
}

/// # Errors
/// `DecodeError` for a malformed value or `24:00:00`.
pub fn decode_time<'py>(py: Python<'py>, raw: &[u8]) -> PyResult<Bound<'py, PyAny>> {
    let t = temporal::decode_time(raw).map_err(|e| decode_error(py, &e.to_string()))?;
    build_time(py, t, None)
}

/// # Errors
/// `DecodeError` for a malformed value, `24:00:00` or an unrepresentable offset.
pub fn decode_timetz<'py>(py: Python<'py>, raw: &[u8]) -> PyResult<Bound<'py, PyAny>> {
    let t = temporal::decode_timetz(raw).map_err(|e| decode_error(py, &e.to_string()))?;
    build_time(py, t.time, Some(fixed_offset(py, t.utc_offset)?))
}

/// `timestamp` is naive and `timestamptz` is UTC-aware.
///
/// # Errors
/// `DecodeError` for ±infinity, a year outside 1..9999 or a malformed value.
pub fn decode_timestamp<'py>(
    py: Python<'py>,
    ty: &Type,
    raw: &[u8],
) -> PyResult<Bound<'py, PyAny>> {
    let aware = *ty == Type::TIMESTAMPTZ;
    let name = if aware { "timestamptz" } else { "timestamp" };
    let DateTime { date, time } =
        temporal::decode_timestamp(name, raw).map_err(|e| decode_error(py, &e.to_string()))?;
    let args = (
        date.year,
        date.month,
        date.day,
        time.hour,
        time.minute,
        time.second,
        time.microsecond,
    );
    let cls = class(py, &DATETIME, "datetime")?;
    if aware {
        let kwargs = PyDict::new(py);
        kwargs.set_item("tzinfo", utc(py)?)?;
        cls.call(args, Some(&kwargs))
    } else {
        cls.call1(args)
    }
}

/// # Errors
/// `DecodeError` for ±infinity, a months component or a span `timedelta` cannot hold.
pub fn decode_interval<'py>(py: Python<'py>, raw: &[u8]) -> PyResult<Bound<'py, PyAny>> {
    let i = temporal::decode_interval(raw).map_err(|e| decode_error(py, &e.to_string()))?;
    class(py, &TIMEDELTA, "timedelta")?
        .call1((i.days, 0, i.microseconds))
        .map_err(|_| {
            decode_error(
                py,
                "interval is too large for a timedelta; cast the column to text to read it",
            )
        })
}
