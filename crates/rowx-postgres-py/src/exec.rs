use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

use pyo3::prelude::*;
use pyo3::types::{PyCFunction, PyList, PyTuple};
use pyo3_async_runtimes::tokio::future_into_py;
use rowx_core::error::CoreError;
use rowx_core::state::{Claim, State};
use rowx_postgres::cancel::{self, Cancel, Hook};
use rowx_postgres::conn::opens_transaction;
use rowx_postgres::error::Error;
use rowx_postgres::exec::{self, Raw, Shape};
use rowx_postgres::pool::{Pool as PgPool, is_stale_plan};
use rowx_postgres::{Row, Type};

use crate::codec::{self, PyParam};
use crate::errors::{PgError, named_to_py_err, to_py_err};
use crate::named::{RowShape, construct};

#[derive(Debug)]
pub struct FetchArgs {
    pub sql: String,
    // one flag byte per column: bit 0 is nullable, bit 1 reads an `interval` as `rowx.postgres.Interval`
    pub mask: Option<Vec<u8>>,
    pub scalar: bool,
    pub limit_one: bool,
    pub arity: Option<usize>,
    // a named row type; `mask`, `scalar` and `arity` are then unused
    pub shape: Option<Py<RowShape>>,
}

const NULLABLE: u8 = 1;
const SPAN: u8 = 2;

/// How a row becomes Python.
#[derive(Debug, Default)]
pub enum RowForm {
    Scalar,
    #[default]
    Tuple,
    Named(Py<RowShape>),
}

/// How a row becomes Python: a bare value, a tuple or a named row, and which columns read an
/// `interval` losslessly.
#[derive(Debug, Default)]
pub struct Layout {
    pub form: RowForm,
    pub spans: Vec<bool>,
    // a named row: the field each column fills (`None` passes through)
    pub targets: Vec<Option<usize>>,
    // a named row built by a validator: the key of each column
    pub keys: Vec<Py<pyo3::types::PyString>>,
}

impl Layout {
    #[must_use]
    pub fn new(scalar: bool, flags: Option<&[u8]>) -> Self {
        Self {
            form: if scalar {
                RowForm::Scalar
            } else {
                RowForm::Tuple
            },
            spans: flags
                .unwrap_or_default()
                .iter()
                .map(|flag| flag & SPAN != 0)
                .collect(),
            targets: Vec::new(),
            keys: Vec::new(),
        }
    }
}

/// The nullability half of the per-column flags.
#[must_use]
pub fn nullable_mask(flags: &[u8]) -> Vec<bool> {
    flags.iter().map(|flag| flag & NULLABLE != 0).collect()
}

/// How a call reaches its connection: the lease of a pooled connection (a released one can no longer
/// claim it) and the pool whose schema epoch a stale-plan error bumps.
#[derive(Debug, Clone, Default)]
pub struct Access {
    pub lease: Option<u64>,
    pub pool: Option<PgPool>,
}

impl Access {
    /// # Errors
    /// `Busy`, `Closed`, `Broken`, or `StaleLease` for a revoked lease.
    pub fn claim(&self, state: &Arc<State>) -> Result<Option<Claim>, CoreError> {
        self.lease
            .map_or_else(|| state.claim(), |lease| state.claim_leased(lease))
    }

    /// A call that arrived while a cancelled operation was settling waits here, outside the GIL.
    ///
    /// # Errors
    /// `InterfaceError` when the connection closed or broke while waiting, or the lease was revoked.
    pub async fn settle_claim(
        &self,
        state: &Arc<State>,
        claim: Option<Claim>,
    ) -> Result<Claim, PgError> {
        match (claim, self.lease) {
            (Some(claim), _) => Ok(claim),
            (None, Some(lease)) => Ok(state.claim_settled_leased(lease).await?),
            (None, None) => Ok(state.claim_settled().await?),
        }
    }

    /// `fail`, and a stale cached plan also bumps the pool's schema epoch.
    pub fn fail(&self, state: &State, err: Error) -> PgError {
        if is_stale_plan(&err)
            && let Some(pool) = &self.pool
        {
            pool.bump_epoch();
        }
        fail(state, err)
    }
}

/// # Errors
/// `ConnectionBusyError`/`InterfaceError` when the connection cannot be claimed or a parameter cannot be extracted.
pub fn fetch<'py>(
    py: Python<'py>,
    cancel: &Cancel,
    access: &Access,
    params: &Bound<'py, PyList>,
    args: FetchArgs,
) -> PyResult<Bound<'py, PyAny>> {
    let params = codec::extract(py, params)?;
    // claimed synchronously so a second call made before the first is awaited fails immediately
    let claim = access
        .claim(&cancel.state)
        .map_err(|e| to_py_err(py, &e.into()))?;
    let cancel = cancel.clone();
    let access = access.clone();
    let hook = Hook::default();
    let watched = hook.clone();
    let future = future_into_py(py, async move {
        run_fetch(cancel, claim, access, hook, params, args).await
    })?;
    watch(&future, watched)?;
    Ok(future)
}

/// The asynchronous half of `fetch`; a pool calls it with `claim: None` once it holds a connection.
///
/// # Errors
/// Any error the query raises, mapped to its Python exception.
pub async fn run_fetch(
    cancel: Cancel,
    claim: Option<Claim>,
    access: Access,
    hook: Hook,
    params: Vec<PyParam>,
    args: FetchArgs,
) -> PyResult<Py<PyAny>> {
    let claim = access.settle_claim(&cancel.state, claim).await?;
    // here and not in the callers, so a pool's fast path records a hand-written BEGIN as well
    note_sql(&cancel.state, &args.sql);
    let inner = Arc::clone(&cancel.backend.conn);
    let layout = Layout::new(args.scalar, args.mask.as_deref());
    let nullable = args.mask.as_deref().map(nullable_mask);
    let named = args
        .shape
        .as_ref()
        .map(|shape| Python::attach(|py| shape.clone_ref(py)));
    let io_shape = named
        .as_ref()
        .map(|shape| Python::attach(|py| shape.clone_ref(py)));
    // the operation owns the request; dropping this future while it runs cancels it
    let pending = cancel::spawn(&cancel, claim, &hook, async move {
        let mut conn = inner.lock().await;
        let shape = driver_shape(
            io_shape.as_ref().map(Py::get),
            nullable.as_deref(),
            args.scalar,
            args.limit_one,
            args.arity,
        );
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
    let (rows, mapping) = pending
        .wait()
        .await
        .map_err(|e| PgError(e.into()))?
        .map_err(|e| fail_named(&access, &cancel.state, e, named.as_ref()))?;
    Python::attach(|py| {
        let layout = match (&named, &mapping, rows.first()) {
            (Some(shape), Some(mapping), Some(first)) => Layout::named(py, shape, mapping, first),
            _ => layout,
        };
        build(py, &rows, &layout)
    })
}

/// The driver's view of what the caller expects: the named shape when there is one (its nullability
/// is per field), otherwise the tuple or scalar arguments.
#[must_use]
pub fn driver_shape<'a>(
    named: Option<&'a RowShape>,
    nullable: Option<&'a [bool]>,
    scalar: bool,
    limit_one: bool,
    arity: Option<usize>,
) -> Shape<'a> {
    named.map_or_else(
        || Shape {
            mask: nullable,
            scalar,
            limit_one,
            arity,
            ..Shape::default()
        },
        |named| Shape {
            mask: Some(named.nullable()),
            limit_one,
            fields: Some(named.specs()),
            matching: named.matching(),
            ..Shape::default()
        },
    )
}

/// `fail`, and a named row's mapping or NULL error is rendered with the row type's name and keys.
pub fn fail_named(
    access: &Access,
    state: &State,
    err: Error,
    shape: Option<&Py<RowShape>>,
) -> PyErr {
    let err = access.fail(state, err);
    Python::attach(|py| match shape {
        Some(shape) => named_to_py_err(py, &err.0, shape.get()),
        None => err.into(),
    })
}

/// # Errors
/// `ConnectionBusyError`/`InterfaceError` when the connection cannot be claimed or a parameter cannot be extracted.
pub fn execute<'py>(
    py: Python<'py>,
    cancel: &Cancel,
    access: &Access,
    params: &Bound<'py, PyList>,
    sql: String,
) -> PyResult<Bound<'py, PyAny>> {
    let params = codec::extract(py, params)?;
    let claim = access
        .claim(&cancel.state)
        .map_err(|e| to_py_err(py, &e.into()))?;
    let cancel = cancel.clone();
    let access = access.clone();
    let hook = Hook::default();
    let watched = hook.clone();
    let future = future_into_py(py, async move {
        run_execute(cancel, claim, access, hook, params, sql).await
    })?;
    watch(&future, watched)?;
    Ok(future)
}

/// The asynchronous half of `execute`.
///
/// # Errors
/// Any error the statement raises, mapped to its Python exception.
pub async fn run_execute(
    cancel: Cancel,
    claim: Option<Claim>,
    access: Access,
    hook: Hook,
    params: Vec<PyParam>,
    sql: String,
) -> PyResult<u64> {
    let claim = access.settle_claim(&cancel.state, claim).await?;
    note_sql(&cancel.state, &sql);
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
        .map_err(|e| access.fail(&cancel.state, e))?;
    Ok(count)
}

/// Runs `sql` as a simple query (`ping`, `reset`). With `reset`, a `ROLLBACK` goes first when the
/// session may be inside a transaction, and the flag clears once the whole batch has succeeded.
///
/// # Errors
/// `ConnectionBusyError`/`InterfaceError` when the connection cannot be claimed.
pub fn simple<'py>(
    py: Python<'py>,
    cancel: &Cancel,
    access: &Access,
    sql: String,
    reset: bool,
) -> PyResult<Bound<'py, PyAny>> {
    let claim = access
        .claim(&cancel.state)
        .map_err(|e| to_py_err(py, &e.into()))?;
    let cancel = cancel.clone();
    let access = access.clone();
    let hook = Hook::default();
    let watched = hook.clone();
    let future = future_into_py(py, async move {
        let claim = access.settle_claim(&cancel.state, claim).await?;
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
            .map_err(|e| access.fail(&cancel.state, e))?;
        Ok(())
    })?;
    watch(&future, watched)?;
    Ok(future)
}

/// What a successful transaction statement does to the helper depth.
#[derive(Debug, Clone, Copy)]
pub enum Depth {
    Enter,
    Leave,
    Keep,
}

/// Runs one statement of the transaction helper over the simple protocol and then adjusts the depth,
/// which it returns.
///
/// # Errors
/// `ConnectionBusyError`/`InterfaceError` when the connection cannot be claimed, or the statement's error.
pub fn txn_step<'py>(
    py: Python<'py>,
    cancel: &Cancel,
    access: &Access,
    sql: String,
    depth: Depth,
) -> PyResult<Bound<'py, PyAny>> {
    let claim = access
        .claim(&cancel.state)
        .map_err(|e| to_py_err(py, &e.into()))?;
    let cancel = cancel.clone();
    let access = access.clone();
    let hook = Hook::default();
    let watched = hook.clone();
    let future = future_into_py(py, async move {
        let claim = access.settle_claim(&cancel.state, claim).await?;
        let inner = Arc::clone(&cancel.backend.conn);
        let pending = cancel::spawn(&cancel, claim, &hook, async move {
            inner.lock().await.simple(&sql).await
        });
        pending
            .wait()
            .await
            .map_err(|e| PgError(e.into()))?
            .map_err(|e| access.fail(&cancel.state, e))?;
        Ok(match depth {
            Depth::Enter => cancel.state.enter_txn(),
            Depth::Leave => cancel.state.leave_txn(),
            Depth::Keep => cancel.state.txn_depth(),
        })
    })?;
    watch(&future, watched)?;
    Ok(future)
}

/// Ends the outermost transaction with `COMMIT` or `ROLLBACK`.
///
/// The statement runs in a task of its own, so dropping the caller neither stops it nor sends a cancel
/// request. A caller that goes away while a `COMMIT` is in flight cannot know the outcome, so the
/// connection is marked broken.
///
/// # Errors
/// `ConnectionBusyError`/`InterfaceError` when the connection cannot be claimed, or the statement's error.
pub fn end_transaction<'py>(
    py: Python<'py>,
    cancel: &Cancel,
    access: &Access,
    commit: bool,
) -> PyResult<Bound<'py, PyAny>> {
    let claim = access
        .claim(&cancel.state)
        .map_err(|e| to_py_err(py, &e.into()))?;
    let cancel = cancel.clone();
    let access = access.clone();
    let task = {
        let (cancel, access) = (cancel.clone(), access.clone());
        cancel.handle.clone().spawn(async move {
            let claim = access
                .settle_claim(&cancel.state, claim)
                .await
                .map_err(|e| e.0)?;
            let conn = cancel.backend.conn.lock().await;
            let result = conn
                .simple(if commit { "COMMIT" } else { "ROLLBACK" })
                .await;
            drop(conn);
            // a rejected COMMIT still ends the transaction; only an IO failure leaves the outcome open
            if matches!(&result, Ok(()) | Err(Error::Db { .. })) {
                cancel.state.clear_maybe_in_txn();
            } else {
                cancel.state.mark_maybe_in_txn();
            }
            drop(claim);
            result
        })
    };
    let state = Arc::clone(&cancel.state);
    let future = future_into_py(py, async move {
        task.await
            .map_err(|e| PgError(Error::Interface(e.to_string())))?
            .map_err(|e| access.fail(&cancel.state, e))?;
        Ok(())
    })?;
    if commit {
        let callback = PyCFunction::new_closure(py, None, None, move |args, _kwargs| {
            if let Ok(done) = args.get_item(0)
                && done.call_method0("cancelled")?.is_truthy()?
            {
                state.mark_broken();
            }
            PyResult::Ok(())
        })?;
        future.call_method1("add_done_callback", (callback,))?;
    }
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

/// Sets `flag` when the Python future is cancelled. A pool checks it right after acquiring, because
/// the Rust future is dropped late: a cancelled waiter must never run its query.
///
/// # Errors
/// Any Python error from registering the callback.
pub fn watch_flag(future: &Bound<'_, PyAny>, flag: Arc<AtomicBool>) -> PyResult<()> {
    let callback = PyCFunction::new_closure(future.py(), None, None, move |args, _kwargs| {
        if let Ok(done) = args.get_item(0)
            && done.call_method0("cancelled")?.is_truthy()?
        {
            flag.store(true, Ordering::Release);
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
    Access::default().settle_claim(state, claim).await
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

fn build(py: Python<'_>, rows: &[Row], layout: &Layout) -> PyResult<Py<PyAny>> {
    let mut out = Vec::with_capacity(rows.len());
    for (index, row) in rows.iter().enumerate() {
        out.push(decode_row(py, row, layout, index)?);
    }
    Ok(PyList::new(py, out)?.into_any().unbind())
}

/// `row_index` is the row's position in the returned rows, counted across stream batches.
///
/// # Errors
/// `DecodeError` for a value that cannot be decoded, `RowConstructError` for a named row whose
/// constructor raised.
pub fn decode_row<'py>(
    py: Python<'py>,
    row: &Row,
    layout: &Layout,
    row_index: usize,
) -> PyResult<Bound<'py, PyAny>> {
    match &layout.form {
        RowForm::Scalar => cell(py, row, 0, layout),
        RowForm::Tuple => {
            let cells = (0..row.len())
                .map(|index| cell(py, row, index, layout))
                .collect::<PyResult<Vec<_>>>()?;
            Ok(PyTuple::new(py, cells)?.into_any())
        }
        RowForm::Named(shape) => construct(py, row, layout, shape.get(), row_index),
    }
}

/// # Errors
/// `DecodeError` for a value that cannot be decoded.
pub fn cell<'py>(
    py: Python<'py>,
    row: &Row,
    index: usize,
    layout: &Layout,
) -> PyResult<Bound<'py, PyAny>> {
    let raw = row
        .try_get::<_, Raw>(index)
        .map_err(|e| to_py_err(py, &Error::from(e)))?;
    let ty = row.columns()[index].type_();
    let as_span = layout.spans.get(index).copied().unwrap_or(false);
    codec::decode_with(py, ty, raw.0, as_span)
}
