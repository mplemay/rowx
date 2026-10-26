use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::PyType;
use rowx_postgres::exec::Param;
use rowx_postgres::wire::money;

use crate::codec::extract::PyParam;
use crate::errors::decode_error;

static MONEY: PyOnceLock<Py<PyType>> = PyOnceLock::new();

/// The `rowx.postgres.Money` class.
///
/// # Errors
/// An import failure of `rowx.postgres`.
pub fn money_class(py: Python<'_>) -> PyResult<&Bound<'_, PyType>> {
    let cls = MONEY.get_or_try_init(py, || {
        Ok::<_, PyErr>(
            py.import("rowx.postgres")?
                .getattr("Money")?
                .cast_into::<PyType>()?
                .unbind(),
        )
    })?;
    Ok(cls.bind(py))
}

/// # Errors
/// `DecodeError` when the value is not 8 bytes.
pub fn decode<'py>(py: Python<'py>, raw: &[u8]) -> PyResult<Bound<'py, PyAny>> {
    let minor_units = money::decode(raw).map_err(|e| decode_error(py, &e.to_string()))?;
    money_class(py)?.call1((minor_units,))
}

/// # Errors
/// A description of why the value cannot be encoded as `money`.
pub fn encode(param: &PyParam) -> Result<Param, String> {
    match param {
        PyParam::Money(minor_units) => Ok(Param::binary(money::encode(*minor_units))),
        _ => Err(format!(
            "a Python {} cannot be encoded as money",
            param.type_name()
        )),
    }
}
