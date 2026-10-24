use std::sync::Arc;

use pyo3::prelude::*;
use pyo3::types::{PyList, PyTuple};
use pyo3_async_runtimes::tokio::future_into_py;
use rowx_pg::conn::Connection as PgConnection;
use rowx_pg::error::Error;
use rowx_pg::exec::{self, Raw, Shape};
use rowx_pg::state::State;
use rowx_pg::{Row, Type};
use tokio::sync::Mutex;

use crate::codec;
use crate::errors::{PgError, to_py_err};

#[derive(Debug)]
pub struct FetchArgs {
    pub sql: String,
    pub mask: Option<Vec<bool>>,
    pub scalar: bool,
    pub limit_one: bool,
    pub arity: Option<usize>,
}

/// # Errors
/// `ConnectionBusyError`/`InterfaceError` when the connection cannot be claimed or a parameter cannot be extracted.
pub fn fetch<'py>(
    py: Python<'py>,
    inner: &Arc<Mutex<PgConnection>>,
    state: &Arc<State>,
    params: &Bound<'py, PyList>,
    args: FetchArgs,
) -> PyResult<Bound<'py, PyAny>> {
    let params = codec::extract(py, params)?;
    // claimed synchronously so a second call made before the first is awaited fails immediately
    let claim = state.claim().map_err(|e| to_py_err(py, &e))?;
    let inner = Arc::clone(inner);
    future_into_py(py, async move {
        let rows = {
            let mut conn = inner.lock().await;
            let shape = Shape {
                mask: args.mask.as_deref(),
                scalar: args.scalar,
                limit_one: args.limit_one,
                arity: args.arity,
            };
            let result = match conn.parts() {
                Ok((client, cache)) => {
                    exec::fetch(client, cache, &args.sql, &shape, |types: &[Type]| {
                        codec::encode_all(&params, types)
                    })
                    .await
                }
                Err(e) => Err(e),
            };
            // Busy → Idle happens before Python sees the result, so `await a(); await b()` never sees Busy
            drop(conn);
            drop(claim);
            result.map_err(PgError)?
        };
        Python::attach(|py| build(py, &rows, args.scalar))
    })
}

/// # Errors
/// `ConnectionBusyError`/`InterfaceError` when the connection cannot be claimed or a parameter cannot be extracted.
pub fn execute<'py>(
    py: Python<'py>,
    inner: &Arc<Mutex<PgConnection>>,
    state: &Arc<State>,
    params: &Bound<'py, PyList>,
    sql: String,
) -> PyResult<Bound<'py, PyAny>> {
    let params = codec::extract(py, params)?;
    let claim = state.claim().map_err(|e| to_py_err(py, &e))?;
    let inner = Arc::clone(inner);
    future_into_py(py, async move {
        let mut conn = inner.lock().await;
        let result = match conn.parts() {
            Ok((client, cache)) => {
                exec::execute(client, cache, &sql, |types: &[Type]| {
                    codec::encode_all(&params, types)
                })
                .await
            }
            Err(e) => Err(e),
        };
        drop(conn);
        drop(claim);
        Ok(result.map_err(PgError)?)
    })
}

fn build(py: Python<'_>, rows: &[Row], scalar: bool) -> PyResult<Py<PyAny>> {
    let mut out = Vec::with_capacity(rows.len());
    for row in rows {
        if scalar {
            out.push(cell(py, row, 0)?);
        } else {
            let cells = (0..row.len())
                .map(|index| cell(py, row, index))
                .collect::<PyResult<Vec<_>>>()?;
            out.push(PyTuple::new(py, cells)?.into_any());
        }
    }
    Ok(PyList::new(py, out)?.into_any().unbind())
}

fn cell<'py>(py: Python<'py>, row: &Row, index: usize) -> PyResult<Bound<'py, PyAny>> {
    let raw = row
        .try_get::<_, Raw>(index)
        .map_err(|e| to_py_err(py, &Error::from(e)))?;
    let ty = row.columns()[index].type_();
    codec::decode(py, ty, raw.0)
}
