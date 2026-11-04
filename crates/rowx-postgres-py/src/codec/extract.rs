use pyo3::buffer::PyBuffer;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{
    PyBool, PyByteArray, PyBytes, PyFloat, PyInt, PyList, PyMemoryView, PyString, PyTuple, PyType,
};

use crate::codec::exact::{field, items};
use crate::codec::extension::cube_class;
use crate::codec::geometric::{class as geo_class, class_name, kind_of_object, point_class};
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
use rowx_postgres::wire::geometric::{Kind, Point, Shape};
use rowx_postgres::wire::network::Addr;
use rowx_postgres::wire::numeric::{DecimalExponent, parse_decimal_str};

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
    // a `decimal.Decimal` read through the unbound `Decimal.__str__`
    Decimal {
        negative: bool,
        digits: Vec<u8>,
        exponent: DecimalExponent,
    },
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
    // one of the `rowx.postgres` geometric dataclasses
    Geometric(Shape),
    // a `rowx.postgres.Cube`: its two corners
    Cube {
        lower: Vec<f64>,
        upper: Vec<f64>,
    },
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
            Self::Decimal { .. } => "Decimal",
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
            Self::Cube { .. } => "Cube",
            Self::Geometric(shape) => class_name(shape.kind()),
            Self::Range { .. } => "Range",
            Self::Bytes(_, name) | Self::Seq(_, _, name) | Self::Other(_, name) => name,
        }
    }
}

const MICROS_PER_SECOND: i64 = 1_000_000;
const SECONDS_PER_DAY: i64 = 86_400;

fn overflow(py: Python<'_>, what: &str) -> PyErr {
    encode_error(py, &format!("the {what} is out of range"))
}

fn int_field(obj: &Bound<'_, PyAny>, cls: &Bound<'_, PyType>, name: &str) -> PyResult<i64> {
    field(obj, cls, name)?.extract::<i64>()
}

// whole days, and the microseconds of the remaining seconds
fn delta_micros(py: Python<'_>, delta: &Bound<'_, PyAny>) -> PyResult<(i64, i64)> {
    let cls = timedelta_class(py)?;
    let days = int_field(delta, cls, "days")?;
    let seconds = int_field(delta, cls, "seconds")?;
    let micros = int_field(delta, cls, "microseconds")?;
    seconds
        .checked_mul(MICROS_PER_SECOND)
        .and_then(|total| total.checked_add(micros))
        .map(|total| (days, total))
        .ok_or_else(|| overflow(py, "timedelta"))
}

// `None` when the value is naive, including a tzinfo whose utcoffset() is None. The tzinfo is user code, so its
// `utcoffset` is called; the base class's own `utcoffset` is the caller, so a subclass of the value cannot
// replace it, and the returned timedelta is read exactly.
fn utc_offset(
    py: Python<'_>,
    obj: &Bound<'_, PyAny>,
    cls: &Bound<'_, PyType>,
) -> PyResult<Option<i64>> {
    let offset = if obj.get_type().is(cls) {
        obj.call_method0("utcoffset")?
    } else {
        cls.getattr("utcoffset")?.call1((obj,))?
    };
    if offset.is_none() {
        return Ok(None);
    }
    let (days, micros) = delta_micros(py, &offset)?;
    days.checked_mul(SECONDS_PER_DAY * MICROS_PER_SECOND)
        .and_then(|total| total.checked_add(micros))
        .map(Some)
        .ok_or_else(|| overflow(py, "UTC offset"))
}

fn ymd(py: Python<'_>, obj: &Bound<'_, PyAny>, cls: &Bound<'_, PyType>) -> PyResult<Ymd> {
    let part = |name: &str| {
        int_field(obj, cls, name)
            .and_then(|value| u32::try_from(value).map_err(|_| overflow(py, "date")))
    };
    Ok(Ymd {
        year: i32::try_from(int_field(obj, cls, "year")?).map_err(|_| overflow(py, "date"))?,
        month: part("month")?,
        day: part("day")?,
    })
}

fn time_micros(py: Python<'_>, obj: &Bound<'_, PyAny>, cls: &Bound<'_, PyType>) -> PyResult<i64> {
    let hour = int_field(obj, cls, "hour")?;
    let minute = int_field(obj, cls, "minute")?;
    let second = int_field(obj, cls, "second")?;
    let micro = int_field(obj, cls, "microsecond")?;
    hour.checked_mul(60)
        .and_then(|v| v.checked_add(minute))
        .and_then(|v| v.checked_mul(60))
        .and_then(|v| v.checked_add(second))
        .and_then(|v| v.checked_mul(MICROS_PER_SECOND))
        .and_then(|v| v.checked_add(micro))
        .ok_or_else(|| overflow(py, "time"))
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
    let cls = range_class(py)?;
    let bound = |name: &str| -> PyResult<Option<Box<PyParam>>> {
        let value = field(obj, cls, name)?;
        if value.is_none() {
            return Ok(None);
        }
        Ok(Some(Box::new(one(py, index, &value, depth + 1)?)))
    };
    Ok(PyParam::Range {
        lower: bound("lower")?,
        upper: bound("upper")?,
        lower_inc: field(obj, cls, "lower_inc")?.extract()?,
        upper_inc: field(obj, cls, "upper_inc")?.extract()?,
        empty: field(obj, cls, "empty")?.extract()?,
    })
}

fn geo_float(
    py: Python<'_>,
    index: usize,
    obj: &Bound<'_, PyAny>,
    cls: &Bound<'_, PyType>,
    name: &str,
) -> PyResult<f64> {
    field(obj, cls, name)
        .and_then(|value| value.extract::<f64>())
        .map_err(|_| encode_error(py, &format!("${}: the {name} is not a number", index + 1)))
}

fn geo_point(py: Python<'_>, index: usize, obj: &Bound<'_, PyAny>) -> PyResult<Point> {
    let cls = point_class(py)?;
    if !obj.is_instance(&cls)? {
        return Err(encode_error(
            py,
            &format!(
                "${}: expected a rowx.postgres.Point, got a Python {}",
                index + 1,
                obj.get_type().name()?
            ),
        ));
    }
    Ok(Point {
        x: geo_float(py, index, obj, &cls, "x")?,
        y: geo_float(py, index, obj, &cls, "y")?,
    })
}

fn geo_field(
    py: Python<'_>,
    index: usize,
    obj: &Bound<'_, PyAny>,
    cls: &Bound<'_, PyType>,
    name: &str,
) -> PyResult<Point> {
    geo_point(py, index, &field(obj, cls, name)?)
}

fn geo_points(
    py: Python<'_>,
    index: usize,
    obj: &Bound<'_, PyAny>,
    cls: &Bound<'_, PyType>,
) -> PyResult<Vec<Point>> {
    items(&field(obj, cls, "points")?)?
        .iter()
        .map(|item| geo_point(py, index, item))
        .collect()
}

fn geometric_param(
    py: Python<'_>,
    index: usize,
    kind: Kind,
    obj: &Bound<'_, PyAny>,
) -> PyResult<PyParam> {
    let cls = geo_class(py, kind)?;
    let shape = match kind {
        Kind::Point => Shape::Point(geo_point(py, index, obj)?),
        Kind::Line => Shape::Line {
            a: geo_float(py, index, obj, &cls, "a")?,
            b: geo_float(py, index, obj, &cls, "b")?,
            c: geo_float(py, index, obj, &cls, "c")?,
        },
        Kind::LineSegment => Shape::LineSegment(
            geo_field(py, index, obj, &cls, "start")?,
            geo_field(py, index, obj, &cls, "end")?,
        ),
        Kind::Box => Shape::Box {
            upper_right: geo_field(py, index, obj, &cls, "upper_right")?,
            lower_left: geo_field(py, index, obj, &cls, "lower_left")?,
        },
        Kind::Path => Shape::Path {
            points: geo_points(py, index, obj, &cls)?,
            closed: field(obj, &cls, "closed")?.extract()?,
        },
        Kind::Polygon => Shape::Polygon(geo_points(py, index, obj, &cls)?),
        Kind::Circle => Shape::Circle {
            center: Point {
                x: geo_float(py, index, obj, &cls, "x")?,
                y: geo_float(py, index, obj, &cls, "y")?,
            },
            radius: geo_float(py, index, obj, &cls, "radius")?,
        },
    };
    Ok(PyParam::Geometric(shape))
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
        let cls = interval_class(py)?;
        let read = |name: &str| {
            int_field(obj, cls, name).map_err(|_| overflow(&format!("the interval {name}")))
        };
        return Ok(Some(PyParam::Interval {
            months: read("months")?,
            days: read("days")?,
            micros: read("microseconds")?,
        }));
    }
    if obj.is_instance(money_class(py)?)? {
        return field(obj, money_class(py)?, "minor_units")?
            .extract::<i64>()
            .map(|minor| Some(PyParam::Money(minor)))
            .map_err(|_| overflow("the money amount"));
    }
    if obj.is_instance(cube_class(py)?)? {
        let cls = cube_class(py)?;
        let corner = |name: &str| -> PyResult<Vec<f64>> {
            items(&field(obj, cls, name)?)?
                .iter()
                .map(PyAnyMethods::extract::<f64>)
                .collect::<PyResult<Vec<_>>>()
                .map_err(|_| {
                    encode_error(
                        py,
                        &format!(
                            "${}: the cube {name} has a coordinate that is not a number",
                            index + 1
                        ),
                    )
                })
        };
        return Ok(Some(PyParam::Cube {
            lower: corner("lower_left")?,
            upper: corner("upper_right")?,
        }));
    }
    Ok(None)
}

// the digits of an int outside i64, through `Decimal` because `str(int)` is capped at 4300 digits by default
// and golden/test_numeric.py pins that a numeric takes more; the `Decimal` constructor reads the int's value, so
// a subclass cannot substitute other digits
fn big_int_digits(py: Python<'_>, index: usize, int: &Bound<'_, PyInt>) -> PyResult<String> {
    decimal_class(py)?
        .call1((int,))
        .and_then(|d| d.str())
        .map(|s| s.to_string())
        .map_err(|e| {
            encode_error(
                py,
                &format!("${}: the int cannot be converted: {e}", index + 1),
            )
        })
}

// the unbound `Decimal.__str__`, looked up once
fn decimal_str_fn(py: Python<'_>) -> PyResult<&Bound<'_, PyAny>> {
    static DECIMAL_STR: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
    let func = DECIMAL_STR.get_or_try_init(py, || {
        Ok::<_, PyErr>(decimal_class(py)?.getattr("__str__")?.unbind())
    })?;
    Ok(func.bind(py))
}

// The value is read through the unbound base-class method, so a `Decimal` subclass cannot override what is
// read; the canonical text is then parsed in Rust with every digit validated. `as_tuple` allocates a
// namedtuple and a digit tuple per value, which doubles the cost of a 100k-element array.
fn decimal_param(py: Python<'_>, index: usize, obj: &Bound<'_, PyAny>) -> PyResult<PyParam> {
    let bad = |what: &str| encode_error(py, &format!("${}: the Decimal {what}", index + 1));
    let text = decimal_str_fn(py)
        .and_then(|func| func.call1((obj,)))
        .and_then(|text| text.extract::<String>())
        .map_err(|e| bad(&format!("cannot be read: {e}")))?;
    let (negative, digits, exponent) =
        parse_decimal_str(&text).map_err(|e| bad(&format!("is malformed: {e}")))?;
    Ok(PyParam::Decimal {
        negative,
        digits,
        exponent,
    })
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
            Err(_) => PyParam::BigInt(big_int_digits(py, index, int)?),
        });
    }
    if let Ok(float) = obj.cast::<PyFloat>() {
        return Ok(PyParam::Float(float.value()));
    }
    if obj.is_instance(decimal_class(py)?)? {
        return decimal_param(py, index, obj);
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
        // the `int` slot, because `bytes` is a property a subclass can override
        let value = field(obj, uuid_class(py)?, "int")?.extract::<u128>()?;
        return Ok(PyParam::Uuid(value.to_be_bytes()));
    }
    if let Some((kind, addr, bits)) =
        read_ip(py, obj).map_err(|e| encode_error(py, &format!("${}: {e}", index + 1)))?
    {
        return Ok(PyParam::Ip { kind, addr, bits });
    }
    // datetime subclasses date, so it has to be checked first
    if obj.is_instance(datetime_class(py)?)? {
        let cls = datetime_class(py)?;
        return Ok(PyParam::DateTime {
            date: ymd(py, obj, cls)?,
            micros: time_micros(py, obj, cls)?,
            offset: utc_offset(py, obj, cls)?,
        });
    }
    if obj.is_instance(date_class(py)?)? {
        return Ok(PyParam::Date(ymd(py, obj, date_class(py)?)?));
    }
    if obj.is_instance(time_class(py)?)? {
        let cls = time_class(py)?;
        return Ok(PyParam::Time {
            micros: time_micros(py, obj, cls)?,
            offset: utc_offset(py, obj, cls)?,
        });
    }
    if obj.is_instance(timedelta_class(py)?)? {
        let (days, micros) = delta_micros(py, obj)?;
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
    if let Some(kind) = kind_of_object(py, obj)? {
        return geometric_param(py, index, kind, obj);
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
