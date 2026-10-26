use pyo3::buffer::PyBuffer;
use pyo3::prelude::*;
use pyo3::types::{
    PyBool, PyByteArray, PyBytes, PyFloat, PyInt, PyList, PyMemoryView, PyString, PyTuple,
};

use crate::codec::money::money_class;
use crate::codec::network::read as read_ip;
use crate::codec::numeric::decimal_class;
use crate::codec::range::range_class;
use crate::codec::temporal::{
    date_class, datetime_class, interval_class, time_class, timedelta_class,
};
use crate::codec::temporal_enc::Ymd;
use crate::codec::uuid::uuid_class;
use crate::errors::encode_error;
use rowx_postgres::wire::network::Addr;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum IpKind {
    Interface,
    Address,
    Network,
}

/// A Python parameter copied out under the GIL so encoding can run without it.
pub enum PyParam {
    Null,
    Bool(bool),
    Int(i64),
    // an int outside i64, kept as its decimal text
    BigInt(String),
    Float(f64),
    // the `str()` of a `decimal.Decimal`
    Decimal(String),
    Str(String),
    // bytes, bytearray or memoryview contents, with the Python type name
    Bytes(Vec<u8>, String),
    Uuid([u8; 16]),
    // an `ipaddress` object: its address, prefix length and which class it was
    Ip {
        kind: IpKind,
        addr: Addr,
        bits: u8,
    },
    // a `date` that is not a `datetime`
    Date(Ymd),
    // microseconds since midnight, and the UTC offset in microseconds when aware
    Time {
        micros: i64,
        offset: Option<i64>,
    },
    DateTime {
        date: Ymd,
        micros: i64,
        offset: Option<i64>,
    },
    // whole days plus the microseconds of the remaining seconds
    Timedelta {
        days: i64,
        micros: i64,
    },
    // a `rowx.postgres.Interval`, with the microseconds as one field
    Interval {
        months: i64,
        days: i64,
        micros: i64,
    },
    // a `rowx.postgres.Money`: its minor units
    Money(i64),
    // a `rowx.postgres.Range`; a `None` bound is an infinite side
    Range {
        lower: Option<Box<Self>>,
        upper: Option<Box<Self>>,
        lower_inc: bool,
        upper_inc: bool,
        empty: bool,
    },
    // a list or tuple, with the original object kept for json and its Python type name
    Seq(Vec<Self>, Py<PyAny>, String),
    // anything without a dedicated variant yet, with its Python type name
    Other(Py<PyAny>, String),
}

impl std::fmt::Debug for PyParam {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "PyParam({}, <redacted>)", self.type_name())
    }
}

impl PyParam {
    #[must_use]
    pub fn type_name(&self) -> &str {
        match self {
            Self::Null => "None",
            Self::Bool(_) => "bool",
            Self::Int(_) | Self::BigInt(_) => "int",
            Self::Float(_) => "float",
            Self::Decimal(_) => "Decimal",
            Self::Str(_) => "str",
            Self::Uuid(_) => "UUID",
            Self::Ip { kind, addr, .. } => match (kind, addr) {
                (IpKind::Interface, Addr::V4(_)) => "IPv4Interface",
                (IpKind::Interface, Addr::V6(_)) => "IPv6Interface",
                (IpKind::Address, Addr::V4(_)) => "IPv4Address",
                (IpKind::Address, Addr::V6(_)) => "IPv6Address",
                (IpKind::Network, Addr::V4(_)) => "IPv4Network",
                (IpKind::Network, Addr::V6(_)) => "IPv6Network",
            },
            Self::Date(_) => "date",
            Self::Time { .. } => "time",
            Self::DateTime { .. } => "datetime",
            Self::Timedelta { .. } => "timedelta",
            Self::Interval { .. } => "Interval",
            Self::Money(_) => "Money",
            Self::Range { .. } => "Range",
            Self::Bytes(_, name) | Self::Seq(_, _, name) | Self::Other(_, name) => name,
        }
    }
}

const MICROS_PER_SECOND: i64 = 1_000_000;

fn delta_micros(delta: &Bound<'_, PyAny>) -> PyResult<(i64, i64)> {
    let days = delta.getattr("days")?.extract::<i64>()?;
    let seconds = delta.getattr("seconds")?.extract::<i64>()?;
    let micros = delta.getattr("microseconds")?.extract::<i64>()?;
    Ok((days, seconds * MICROS_PER_SECOND + micros))
}

// `None` when the value is naive, including a tzinfo whose utcoffset() is None
fn utc_offset(obj: &Bound<'_, PyAny>) -> PyResult<Option<i64>> {
    let offset = obj.call_method0("utcoffset")?;
    if offset.is_none() {
        return Ok(None);
    }
    let (days, micros) = delta_micros(&offset)?;
    Ok(Some(days * 86_400 * MICROS_PER_SECOND + micros))
}

fn ymd(obj: &Bound<'_, PyAny>) -> PyResult<Ymd> {
    Ok(Ymd {
        year: obj.getattr("year")?.extract()?,
        month: obj.getattr("month")?.extract()?,
        day: obj.getattr("day")?.extract()?,
    })
}

fn time_micros(obj: &Bound<'_, PyAny>) -> PyResult<i64> {
    let hour = obj.getattr("hour")?.extract::<i64>()?;
    let minute = obj.getattr("minute")?.extract::<i64>()?;
    let second = obj.getattr("second")?.extract::<i64>()?;
    let micro = obj.getattr("microsecond")?.extract::<i64>()?;
    Ok(((hour * 60 + minute) * 60 + second) * MICROS_PER_SECOND + micro)
}

// postgres arrays have at most 6 dimensions; deeper nesting is left opaque, which also bounds
// the recursion for a self-referential list
const MAX_SEQ_DEPTH: usize = 16;

fn range_param(
    py: Python<'_>,
    index: usize,
    obj: &Bound<'_, PyAny>,
    depth: usize,
) -> PyResult<PyParam> {
    let bound = |name: &str| -> PyResult<Option<Box<PyParam>>> {
        let value = obj.getattr(name)?;
        if value.is_none() {
            return Ok(None);
        }
        Ok(Some(Box::new(one(py, index, &value, depth + 1)?)))
    };
    Ok(PyParam::Range {
        lower: bound("lower")?,
        upper: bound("upper")?,
        lower_inc: obj.getattr("lower_inc")?.extract()?,
        upper_inc: obj.getattr("upper_inc")?.extract()?,
        empty: obj.getattr("empty")?.extract()?,
    })
}

// the `rowx.postgres` value classes
fn value_class(py: Python<'_>, index: usize, obj: &Bound<'_, PyAny>) -> PyResult<Option<PyParam>> {
    let overflow = |what: &str| {
        encode_error(
            py,
            &format!("${}: {what} is outside the int8 range", index + 1),
        )
    };
    if obj.is_instance(interval_class(py)?)? {
        let field = |name: &str| {
            obj.getattr(name)?
                .extract::<i64>()
                .map_err(|_| overflow(&format!("the interval {name}")))
        };
        return Ok(Some(PyParam::Interval {
            months: field("months")?,
            days: field("days")?,
            micros: field("microseconds")?,
        }));
    }
    if obj.is_instance(money_class(py)?)? {
        return obj
            .getattr("minor_units")?
            .extract::<i64>()
            .map(|minor| Some(PyParam::Money(minor)))
            .map_err(|_| overflow("the money amount"));
    }
    Ok(None)
}

fn one(py: Python<'_>, index: usize, obj: &Bound<'_, PyAny>, depth: usize) -> PyResult<PyParam> {
    if obj.is_none() {
        return Ok(PyParam::Null);
    }
    // bool is a subclass of int, so it has to be checked first
    if obj.is_instance_of::<PyBool>() {
        return Ok(PyParam::Bool(obj.extract()?));
    }
    if let Ok(int) = obj.cast::<PyInt>() {
        return Ok(match int.extract::<i64>() {
            Ok(value) => PyParam::Int(value),
            // str() of an int is capped at 4300 digits by default; Decimal's conversion is not
            Err(_) => PyParam::BigInt(decimal_class(py)?.call1((int,))?.str()?.to_string()),
        });
    }
    if let Ok(float) = obj.cast::<PyFloat>() {
        return Ok(PyParam::Float(float.value()));
    }
    if obj.is_instance(decimal_class(py)?)? {
        return Ok(PyParam::Decimal(obj.str()?.to_string()));
    }
    if obj.is_instance_of::<PyBytes>()
        || obj.is_instance_of::<PyByteArray>()
        || obj.is_instance_of::<PyMemoryView>()
    {
        let name = obj.get_type().name()?.to_string();
        let data = if obj.is_instance_of::<PyMemoryView>() {
            // a memoryview of any element format or layout is copied as its raw bytes
            obj.call_method0("tobytes")
                .and_then(|raw| Ok(raw.cast_into::<PyBytes>()?.as_bytes().to_vec()))
        } else {
            PyBuffer::<u8>::get(obj).and_then(|buf| buf.to_vec(py))
        };
        return data.map(|data| PyParam::Bytes(data, name)).map_err(|e| {
            encode_error(py, &format!("${}: the buffer is unusable: {e}", index + 1))
        });
    }
    if obj.is_instance(uuid_class(py)?)? {
        let raw = obj.getattr("bytes")?.extract::<Vec<u8>>()?;
        if let Ok(bytes) = <[u8; 16]>::try_from(raw) {
            return Ok(PyParam::Uuid(bytes));
        }
    }
    if let Some((kind, addr, bits)) =
        read_ip(py, obj).map_err(|e| encode_error(py, &format!("${}: {e}", index + 1)))?
    {
        return Ok(PyParam::Ip { kind, addr, bits });
    }
    // datetime subclasses date, so it has to be checked first
    if obj.is_instance(datetime_class(py)?)? {
        return Ok(PyParam::DateTime {
            date: ymd(obj)?,
            micros: time_micros(obj)?,
            offset: utc_offset(obj)?,
        });
    }
    if obj.is_instance(date_class(py)?)? {
        return Ok(PyParam::Date(ymd(obj)?));
    }
    if obj.is_instance(time_class(py)?)? {
        return Ok(PyParam::Time {
            micros: time_micros(obj)?,
            offset: utc_offset(obj)?,
        });
    }
    if obj.is_instance(timedelta_class(py)?)? {
        let (days, micros) = delta_micros(obj)?;
        return Ok(PyParam::Timedelta { days, micros });
    }
    if let Some(param) = value_class(py, index, obj)? {
        return Ok(param);
    }
    if let Ok(text) = obj.cast::<PyString>() {
        return text
            .to_str()
            .map(|s| PyParam::Str(s.to_owned()))
            .map_err(|e| {
                encode_error(
                    py,
                    &format!("${}: the str cannot be encoded as UTF-8: {e}", index + 1),
                )
            });
    }
    if depth < MAX_SEQ_DEPTH && obj.is_instance(range_class(py)?)? {
        return range_param(py, index, obj, depth);
    }
    let name = obj.get_type().name()?.to_string();
    if depth < MAX_SEQ_DEPTH && (obj.is_instance_of::<PyList>() || obj.is_instance_of::<PyTuple>())
    {
        let items = obj
            .try_iter()?
            .map(|item| one(py, index, &item?, depth + 1))
            .collect::<PyResult<Vec<_>>>()?;
        return Ok(PyParam::Seq(items, obj.clone().unbind(), name));
    }
    Ok(PyParam::Other(obj.clone().unbind(), name))
}

/// # Errors
/// `EncodeError` for a `str` that is not valid UTF-8 (a lone surrogate).
pub fn extract(py: Python<'_>, params: &Bound<'_, PyList>) -> PyResult<Vec<PyParam>> {
    params
        .iter()
        .enumerate()
        .map(|(index, obj)| one(py, index, &obj, 0))
        .collect()
}
