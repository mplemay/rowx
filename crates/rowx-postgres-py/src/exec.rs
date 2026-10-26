use std::sync::Arc;

use pyo3::prelude::*;
use pyo3::types::{PyCFunction, PyList, PyTuple};
use pyo3_async_runtimes::tokio::future_into_py;
use rowx_core::error::CoreError;
use rowx_core::state::{Claim, State};
use rowx_postgres::cancel::{self, Cancel, Hook};
use rowx_postgres::conn::opens_transaction;
use rowx_postgres::error::Error;
use rowx_postgres::exec::{self, Raw, Shape};
use rowx_postgres::{Row, Type};

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
    cancel: &Cancel,
    params: &Bound<'py, PyList>,
    args: FetchArgs,
) -> PyResult<Bound<'py, PyAny>> {
    let params = codec::extract(py, params)?;
    // claimed synchronously so a second call made before the first is awaited fails immediately
    let claim = cancel.state.claim().map_err(|e| to_py_err(py, &e.into()))?;
    note_sql(&cancel.state, &args.sql);
    let cancel = cancel.clone();
    let hook = Hook::default();
    let watched = hook.clone();
    let future = future_into_py(py, async move {
        let claim = settle_claim(&cancel.state, claim).await?;
        let inner = Arc::clone(&cancel.backend.conn);
        let scalar = args.scalar;
        // the operation owns the request; dropping this future while it runs cancels it
        let pending = cancel::spawn(&cancel, claim, &hook, async move {
            let mut conn = inner.lock().await;
            let shape = Shape {
                mask: args.mask.as_deref(),
                scalar: args.scalar,
                limit_one: args.limit_one,
                arity: args.arity,
            };
            match conn.parts() {
                Ok((client, cache)) => {
                    exec::fetch(client, cache, &args.sql, &shape, |types: &[Type]| {
                        codec::encode_all(&params, types)
                    })
                    .await
                }
                Err(e) => Err(e),
            }
        });
        let rows = pending
            .wait()
            .await
            .map_err(|e| PgError(e.into()))?
            .map_err(|e| fail(&cancel.state, e))?;
        Python::attach(|py| build(py, &rows, scalar))
    })?;
    watch(&future, watched)?;
    Ok(future)
}

/// # Errors
/// `ConnectionBusyError`/`InterfaceError` when the connection cannot be claimed or a parameter cannot be extracted.
pub fn execute<'py>(
    py: Python<'py>,
    cancel: &Cancel,
    params: &Bound<'py, PyList>,
    sql: String,
) -> PyResult<Bound<'py, PyAny>> {
    let params = codec::extract(py, params)?;
    let claim = cancel.state.claim().map_err(|e| to_py_err(py, &e.into()))?;
    note_sql(&cancel.state, &sql);
    let cancel = cancel.clone();
    let hook = Hook::default();
    let watched = hook.clone();
    let future = future_into_py(py, async move {
        let claim = settle_claim(&cancel.state, claim).await?;
        let inner = Arc::clone(&cancel.backend.conn);
        let pending = cancel::spawn(&cancel, claim, &hook, async move {
            let mut conn = inner.lock().await;
            match conn.parts() {
                Ok((client, cache)) => {
                    exec::execute(client, cache, &sql, |types: &[Type]| {
                        codec::encode_all(&params, types)
                    })
                    .await
                }
                Err(e) => Err(e),
            }
        });
        let count = pending
            .wait()
            .await
            .map_err(|e| PgError(e.into()))?
            .map_err(|e| fail(&cancel.state, e))?;
        Ok(count)
    })?;
    watch(&future, watched)?;
    Ok(future)
}

/// Runs `sql` as a simple query (`ping`, `reset`). With `reset`, a `ROLLBACK` goes first when the
/// session may be inside a transaction, and the flag clears once the whole batch has succeeded.
///
/// # Errors
/// `ConnectionBusyError`/`InterfaceError` when the connection cannot be claimed.
pub fn simple<'py>(
    py: Python<'py>,
    cancel: &Cancel,
    sql: String,
    reset: bool,
) -> PyResult<Bound<'py, PyAny>> {
    let claim = cancel.state.claim().map_err(|e| to_py_err(py, &e.into()))?;
    let cancel = cancel.clone();
    let hook = Hook::default();
    let watched = hook.clone();
    let future = future_into_py(py, async move {
        let claim = settle_claim(&cancel.state, claim).await?;
        let inner = Arc::clone(&cancel.backend.conn);
        let state = Arc::clone(&cancel.state);
        let pending = cancel::spawn(&cancel, claim, &hook, async move {
            let conn = inner.lock().await;
            // an unconditional ROLLBACK would log a server WARNING on every idle reset
            let sql = if reset && state.maybe_in_txn() {
                format!("ROLLBACK; {sql}")
            } else {
                sql
            };
            let result = conn.simple(&sql).await;
            drop(conn);
            result?;
            if reset {
                state.clear_maybe_in_txn();
            }
            Ok::<(), Error>(())
        });
        pending
            .wait()
            .await
            .map_err(|e| PgError(e.into()))?
            .map_err(|e| fail(&cancel.state, e))?;
        Ok(())
    })?;
    watch(&future, watched)?;
    Ok(future)
}

/// Sets the "maybe in a transaction" flag when `sql` opens one by hand.
pub fn note_sql(state: &State, sql: &str) {
    if opens_transaction(sql) {
        state.mark_maybe_in_txn();
    }
}

/// asyncio runs done callbacks before the awaiting task resumes, while the Rust future is only dropped
/// later on another thread; settling here keeps `cancel(); await next()` from seeing Busy
///
/// # Errors
/// Any Python error from registering the callback.
pub fn watch(future: &Bound<'_, PyAny>, hook: Hook) -> PyResult<()> {
    let callback = PyCFunction::new_closure(future.py(), None, None, move |args, _kwargs| {
        if let Ok(done) = args.get_item(0)
            && done.call_method0("cancelled")?.is_truthy()?
        {
            hook.fire();
        }
        PyResult::Ok(())
    })?;
    future.call_method1("add_done_callback", (callback,))?;
    Ok(())
}

/// A call that arrived while a cancelled operation was settling waits here, outside the GIL.
///
/// # Errors
/// `InterfaceError` when the connection closed or broke while waiting.
pub async fn settle_claim(state: &Arc<State>, claim: Option<Claim>) -> Result<Claim, PgError> {
    match claim {
        Some(claim) => Ok(claim),
        None => Ok(state.claim_settled().await?),
    }
}

// a dead connection stays dead; every other error leaves the state to the claim's drop (Idle)
pub fn fail(state: &State, err: Error) -> PgError {
    if err == Error::Core(CoreError::Broken) {
        state.mark_broken();
    }
    // 25P02: the session is inside an aborted transaction
    if matches!(&err, Error::Db { sqlstate, .. } if &**sqlstate == "25P02") {
        state.mark_maybe_in_txn();
    }
    PgError(err)
}

fn build(py: Python<'_>, rows: &[Row], scalar: bool) -> PyResult<Py<PyAny>> {
    let mut out = Vec::with_capacity(rows.len());
    for row in rows {
        out.push(decode_row(py, row, scalar)?);
    }
    Ok(PyList::new(py, out)?.into_any().unbind())
}

/// # Errors
/// `DecodeError` for a value that cannot be decoded.
pub fn decode_row<'py>(py: Python<'py>, row: &Row, scalar: bool) -> PyResult<Bound<'py, PyAny>> {
    if scalar {
        return cell(py, row, 0);
    }
    let cells = (0..row.len())
        .map(|index| cell(py, row, index))
        .collect::<PyResult<Vec<_>>>()?;
    Ok(PyTuple::new(py, cells)?.into_any())
}

fn cell<'py>(py: Python<'py>, row: &Row, index: usize) -> PyResult<Bound<'py, PyAny>> {
    let raw = row
        .try_get::<_, Raw>(index)
        .map_err(|e| to_py_err(py, &Error::from(e)))?;
    let ty = row.columns()[index].type_();
    codec::decode(py, ty, raw.0)
}
