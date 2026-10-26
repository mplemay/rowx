use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyDict, PyType};
use rowx_postgres::exec::Param;
use rowx_postgres::types::{Format, Matcher, TYPE_TABLE};
use rowx_postgres::wire::range::{self, Bound as RangeBound, Range};
use rowx_postgres::{Kind, Type};

use crate::codec::extract::PyParam;
use crate::codec::{decode as decode_value, encode_one};
use crate::errors::decode_error;

static RANGE: PyOnceLock<Py<PyType>> = PyOnceLock::new();

/// The `rowx.postgres.Range` class.
///
/// # Errors
/// An import failure of `rowx.postgres`.
pub fn range_class(py: Python<'_>) -> PyResult<&Bound<'_, PyType>> {
    let cls = RANGE.get_or_try_init(py, || {
        Ok::<_, PyErr>(
            py.import("rowx.postgres")?
                .getattr("Range")?
                .cast_into::<PyType>()?
                .unbind(),
        )
    })?;
    Ok(cls.bind(py))
}

fn subtype<'a>(py: Python<'_>, ty: &'a Type) -> PyResult<&'a Type> {
    match ty.kind() {
        Kind::Range(subtype) => Ok(subtype),
        _ => Err(decode_error(
            py,
            &format!("{} is not a range type", ty.name()),
        )),
    }
}

fn bound_value<'py>(
    py: Python<'py>,
    subtype: &Type,
    bound: &RangeBound<'_>,
) -> PyResult<Bound<'py, PyAny>> {
    Ok(match bound.value() {
        // an unbounded side is `None`, and a bound that is NULL on the wire cannot occur
        None => py.None().into_bound(py),
        Some(raw) => decode_value(py, subtype, Some(raw))?,
    })
}

/// # Errors
/// `DecodeError` when the range is malformed or a bound cannot be decoded.
pub fn decode<'py>(py: Python<'py>, ty: &Type, raw: &[u8]) -> PyResult<Bound<'py, PyAny>> {
    let subtype = subtype(py, ty)?;
    let parsed = range::decode(raw)
        .map_err(|e| decode_error(py, &format!("{} is malformed: {e}", ty.name())))?;
    let kwargs = PyDict::new(py);
    let (lower, upper) = match parsed {
        Range::Empty => {
            kwargs.set_item("empty", true)?;
            (py.None().into_bound(py), py.None().into_bound(py))
        }
        Range::Bounds { lower, upper } => {
            kwargs.set_item("lower_inc", lower.is_inclusive())?;
            kwargs.set_item("upper_inc", upper.is_inclusive())?;
            (
                bound_value(py, subtype, &lower)?,
                bound_value(py, subtype, &upper)?,
            )
        }
    };
    range_class(py)?.call((lower, upper), Some(&kwargs))
}

fn side(
    subtype: &Type,
    value: Option<&PyParam>,
    inclusive: bool,
    which: &str,
) -> Result<(Side, Option<Param>), String> {
    let Some(param) = value.filter(|p| !matches!(p, PyParam::Null)) else {
        return Ok((Side::Unbounded, None));
    };
    let encoded = encode_one(subtype, param).map_err(|msg| format!("{which} bound: {msg}"))?;
    Ok((
        if inclusive {
            Side::Inclusive
        } else {
            Side::Exclusive
        },
        Some(encoded),
    ))
}

#[derive(Clone, Copy)]
enum Side {
    Unbounded,
    Inclusive,
    Exclusive,
}

fn bound_of(side: Side, param: Option<&Param>) -> RangeBound<'_> {
    match (side, param.and_then(Param::value)) {
        (Side::Inclusive, Some(raw)) => RangeBound::Inclusive(raw),
        (Side::Exclusive, Some(raw)) => RangeBound::Exclusive(raw),
        _ => RangeBound::Unbounded,
    }
}

/// Binary when both bounds encode as binary, otherwise a text literal.
///
/// # Errors
/// A description of why the value cannot be encoded as `ty`.
pub fn encode(ty: &Type, param: &PyParam) -> Result<Param, String> {
    let Kind::Range(subtype) = ty.kind() else {
        return Err(format!("{} is not a range type", ty.name()));
    };
    let PyParam::Range {
        lower,
        upper,
        lower_inc,
        upper_inc,
        empty,
    } = param
    else {
        return Err(format!(
            "a Python {} cannot be encoded as {}",
            param.type_name(),
            ty.name()
        ));
    };
    // the form follows the subtype, not the bounds, so an array of ranges never mixes binary and text
    // elements, which is what an unbounded or empty range would otherwise do
    let as_text = TYPE_TABLE.iter().any(|info| {
        info.matcher == Matcher::RangeKind(subtype.oid()) && info.encode_format == Format::Text
    });
    if *empty {
        return if as_text {
            Ok(Param::text(
                range::encode_literal(&Range::Empty).into_bytes(),
            ))
        } else {
            range::encode(&Range::Empty)
                .map(Param::binary)
                .map_err(|e| e.to_string())
        };
    }
    let (lower_side, lower_param) = side(subtype, lower.as_deref(), *lower_inc, "lower")?;
    let (upper_side, upper_param) = side(subtype, upper.as_deref(), *upper_inc, "upper")?;
    let value = Range::Bounds {
        lower: bound_of(lower_side, lower_param.as_ref()),
        upper: bound_of(upper_side, upper_param.as_ref()),
    };
    // numeric travels as text, so a range of it does too, and the server parses the literal
    if as_text {
        return Ok(Param::text(range::encode_literal(&value).into_bytes()));
    }
    range::encode(&value)
        .map(Param::binary)
        .map_err(|e| e.to_string())
}
