use pyo3::prelude::*;
use pyo3::types::PyList;
use rowx_pg::wire::array;
use rowx_pg::{Kind, Type};

use crate::codec;
use crate::errors::decode_error;

// builds one nesting level from the flat row-major elements, consuming `cursor` as it goes
fn build<'py>(
    py: Python<'py>,
    elem_ty: &Type,
    dims: &[usize],
    elements: &[Option<&[u8]>],
    cursor: &mut usize,
) -> PyResult<Bound<'py, PyAny>> {
    let Some((&len, rest)) = dims.split_first() else {
        let raw = elements.get(*cursor).copied().flatten();
        *cursor += 1;
        return codec::decode(py, elem_ty, raw);
    };
    let list = PyList::empty(py);
    for _ in 0..len {
        list.append(build(py, elem_ty, rest, elements, cursor)?)?;
    }
    Ok(list.into_any())
}

/// # Errors
/// `DecodeError` when the array is malformed or an element cannot be decoded.
pub fn decode<'py>(py: Python<'py>, ty: &Type, raw: &[u8]) -> PyResult<Bound<'py, PyAny>> {
    let Kind::Array(elem_ty) = ty.kind() else {
        return Err(decode_error(
            py,
            &format!("{} is not an array type", ty.name()),
        ));
    };
    let parsed = array::decode(raw)
        .map_err(|e| decode_error(py, &format!("{} is malformed: {e}", ty.name())))?;
    if parsed.dims.is_empty() {
        return Ok(PyList::empty(py).into_any());
    }
    let mut cursor = 0;
    build(py, elem_ty, &parsed.dims, &parsed.elements, &mut cursor)
}
