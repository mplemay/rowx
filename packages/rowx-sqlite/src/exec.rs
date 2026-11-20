use std::future::Future;
use std::sync::Arc;

use pyo3::prelude::*;
use pyo3::types::{PyCFunction, PyList};
use pyo3_async_runtimes::tokio::future_into_py;
use rowx_core::error::CoreError;
use rowx_core::state::State;
use rowx_py_core::exec::watch;
use rowx_py_core::row::{self, Layout, nullable_mask};
use rowx_sqlite::cancel::{self, Hook};
use rowx_sqlite::conn::Connection as SqliteConnection;
use rowx_sqlite::error::Error;
use rowx_sqlite::exec::{Fetched, Param, Row, Shape};
use rowx_sqlite::value::Scalar;

use crate::codec;
use crate::conn::Core;
use crate::errors::{Failure, SqliteError, interface_error, named_to_py_err, to_py_err};
use crate::named::{RowShape, layout_named};

#[derive(Debug)]
pub struct FetchArgs {
    pub sql: String,
    // one flag byte per column: bit 0 is nullable
    pub mask: Option<Vec<u8>>,
    pub scalar: bool,
    pub limit_one: bool,
    pub arity: Option<usize>,
    // a named row type; `mask`, `scalar` and `arity` are then unused
    pub shape: Option<Py<RowShape>>,
}

/// What a successful transaction statement does to the helper depth.
#[derive(Debug, Clone, Copy)]
pub enum Depth {
    Enter,
    Leave,
    Keep,
}

// a dead connection stays dead; every other error leaves the state to the claim's drop (Idle)
fn fail(state: &State, failure: Failure) -> Failure {
    if matches!(
        &failure,
        Failure::Driver {
            error: Error::Core(CoreError::Broken),
            ..
        }
    ) {
        state.mark_broken();
    }
    failure
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
        |named| named.data().shape(limit_one),
    )
}

/// `fail`, and a named row's mapping or NULL error is rendered with the row type's name and keys.
pub fn fail_named(state: &State, failure: Failure, shape: Option<&Py<RowShape>>) -> PyErr {
    let failure = fail(state, failure);
    Python::attach(|py| {
        shape.map_or_else(
            || to_py_err(py, &failure),
            |shape| named_to_py_err(py, &failure, shape.get()),
        )
    })
}

// claims the connection at once, so a second call made before the first is awaited fails immediately
fn claim(py: Python<'_>, core: &Arc<Core>) -> PyResult<(Arc<Core>, Hook, Hook)> {
    let claim = core
        .access
        .claim(&core.cancel.state)
        .map_err(|e| to_py_err(py, &Failure::from(e)))?;
    let hook = Hook::new(claim);
    let watched = hook.clone();
    Ok((Arc::clone(core), hook, watched))
}

// one operation on the worker: it owns the claim, and dropping the awaiting call cancels it
fn run<'py, T, Fut, R>(
    py: Python<'py>,
    core: &Arc<Core>,
    make: impl FnOnce(Arc<Core>) -> Fut + Send + 'static,
    after: impl FnOnce(&Core, T) -> R + Send + 'static,
) -> PyResult<Bound<'py, PyAny>>
where
    Fut: Future<Output = Result<T, Failure>> + Send + 'static,
    T: Send + 'static,
    R: for<'a> IntoPyObject<'a> + Send + 'static,
{
    let (core, hook, watched) = claim(py, core)?;
    let future = future_into_py(py, async move {
        core.access
            .reserve(&core.cancel.state, &hook)
            .await
            .map_err(SqliteError::from)?;
        let pending = cancel::spawn(&core.cancel, &hook, make(Arc::clone(&core)))
            .map_err(SqliteError::from)?;
        let out = pending
            .wait()
            .await
            .map_err(SqliteError::from)?
            .map_err(|failure| SqliteError(fail(&core.cancel.state, failure)))?;
        Ok(after(&core, out))
    })?;
    watch(&future, watched)?;
    Ok(future)
}

/// # Errors
/// `ConnectionBusyError`/`InterfaceError` when the connection cannot be claimed, a parameter cannot be
/// extracted or there are too many of them.
pub fn fetch<'py>(
    py: Python<'py>,
    core: &Arc<Core>,
    params: &Bound<'py, PyList>,
    args: FetchArgs,
) -> PyResult<Bound<'py, PyAny>> {
    let params = codec::extract_all(py, params)?;
    core.check_params(py, params.len())?;
    let (core, hook, watched) = claim(py, core)?;
    let future = future_into_py(py, async move { run_fetch(core, hook, params, args).await })?;
    watch(&future, watched)?;
    Ok(future)
}

async fn run_fetch(
    core: Arc<Core>,
    hook: Hook,
    params: Vec<Param>,
    args: FetchArgs,
) -> PyResult<Py<PyAny>> {
    core.access
        .reserve(&core.cancel.state, &hook)
        .await
        .map_err(SqliteError::from)?;
    let layout = Layout::new(args.scalar, args.mask.as_deref());
    let nullable = args.mask.as_deref().map(nullable_mask);
    let (named, io_shape) = Python::attach(|py| {
        (
            args.shape.as_ref().map(|shape| shape.clone_ref(py)),
            args.shape.as_ref().map(|shape| shape.clone_ref(py)),
        )
    });
    let task = Arc::clone(&core);
    let pending = cancel::spawn(&core.cancel, &hook, async move {
        if task.aborted() {
            return Err(Failure::Aborted);
        }
        let conn = task.conn.lock().await;
        let shape = driver_shape(
            io_shape.as_ref().map(Py::get),
            nullable.as_deref(),
            args.scalar,
            args.limit_one,
            args.arity,
        );
        conn.fetch(&args.sql, params, &shape)
            .await
            .map_err(|err| Failure::at(err, &args.sql))
    })
    .map_err(SqliteError::from)?;
    let fetched = pending
        .wait()
        .await
        .map_err(SqliteError::from)?
        .map_err(|failure| fail_named(&core.cancel.state, failure, named.as_ref()))?;
    Python::attach(|py| build(py, &fetched, named.as_ref(), layout))
}

fn build(
    py: Python<'_>,
    fetched: &Fetched,
    named: Option<&Py<RowShape>>,
    layout: Layout,
) -> PyResult<Py<PyAny>> {
    let layout = match (named, &fetched.mapping, fetched.rows.is_empty()) {
        (Some(shape), Some(mapping), false) => {
            layout_named(py, shape.get(), mapping, &fetched.columns)?
        }
        // a mapping is always published with a named shape; running on without one would silently
        // decode tuples
        (Some(_), None, false) => {
            return Err(interface_error(
                py,
                "a named row type was given no column mapping",
            ));
        }
        _ => layout,
    };
    let mut out = Vec::with_capacity(fetched.rows.len());
    for (index, row) in fetched.rows.iter().enumerate() {
        out.push(decode_row(py, row, &layout, index)?);
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
    row::decode_row(
        py,
        row.len(),
        |index| {
            row.get(index).map_or_else(
                || {
                    Err(interface_error(
                        py,
                        "a row has fewer columns than its layout",
                    ))
                },
                |cell| codec::to_py(py, cell.as_ref()),
            )
        },
        layout,
        row_index,
    )
}

/// # Errors
/// `ConnectionBusyError`/`InterfaceError` when the connection cannot be claimed, a parameter cannot be
/// extracted or there are too many of them.
pub fn execute<'py>(
    py: Python<'py>,
    core: &Arc<Core>,
    params: &Bound<'py, PyList>,
    sql: String,
) -> PyResult<Bound<'py, PyAny>> {
    let params = codec::extract_all(py, params)?;
    core.check_params(py, params.len())?;
    run(
        py,
        core,
        move |core| async move {
            if core.aborted() {
                return Err(Failure::Aborted);
            }
            let conn = core.conn.lock().await;
            conn.execute(&sql, params)
                .await
                .map_err(|err| Failure::at(err, &sql))
        },
        |_, changed| changed,
    )
}

/// # Errors
/// `ConnectionBusyError`/`InterfaceError` when the connection cannot be claimed.
pub fn ping<'py>(py: Python<'py>, core: &Arc<Core>) -> PyResult<Bound<'py, PyAny>> {
    run(
        py,
        core,
        |core| async move { core.conn.lock().await.ping().await.map_err(Failure::from) },
        |_, ()| (),
    )
}

/// The `BEGIN` statement for a mode. A read-only transaction is always deferred, and the default is
/// immediate so a write never has to upgrade a read lock.
///
/// # Errors
/// `InterfaceError` for an unknown mode, or for a mode that a read-only transaction cannot have.
pub fn begin_sql(mode: Option<&str>, read_only: bool) -> Result<&'static str, Failure> {
    match (mode, read_only) {
        (None | Some("immediate"), false) => Ok("BEGIN IMMEDIATE"),
        (None | Some("deferred"), true) | (Some("deferred"), false) => Ok("BEGIN DEFERRED"),
        (Some("exclusive"), false) => Ok("BEGIN EXCLUSIVE"),
        (Some(_), true) => Err(Error::Interface(
            "a read-only transaction is always deferred; drop the mode".to_owned(),
        )
        .into()),
        (Some(other), false) => Err(Error::Interface(format!(
            "mode must be \"deferred\", \"immediate\" or \"exclusive\", not {other:?}"
        ))
        .into()),
    }
}

// `PRAGMA query_only` is what makes a transaction read-only: the pragma is set for the transaction's
// length and put back when it ends
async fn set_query_only(core: &Core, conn: &SqliteConnection) -> Result<(), Failure> {
    let rows = conn
        .fetch("PRAGMA query_only", Vec::new(), &Shape::default())
        .await?
        .rows;
    let was_on = matches!(
        rows.first().and_then(|row| row.first()),
        Some(Some(Scalar::Integer(1)))
    );
    conn.simple("PRAGMA query_only = ON").await?;
    core.remember_query_only(was_on);
    Ok(())
}

async fn restore_query_only(core: &Core, conn: &SqliteConnection) {
    if let Some(was_on) = core.take_query_only() {
        let sql = if was_on {
            "PRAGMA query_only = ON"
        } else {
            "PRAGMA query_only = OFF"
        };
        // the transaction is over either way, and a connection that cannot take a pragma is broken
        let _ = conn.simple(sql).await;
    }
}

/// Opens the outermost transaction; resolves to the depth.
///
/// # Errors
/// `ConnectionBusyError`/`InterfaceError` when the connection cannot be claimed, or the statement's error.
pub fn begin<'py>(
    py: Python<'py>,
    core: &Arc<Core>,
    sql: &'static str,
    read_only: bool,
) -> PyResult<Bound<'py, PyAny>> {
    run(
        py,
        core,
        move |core| async move {
            let conn = core.conn.lock().await;
            if read_only {
                set_query_only(&core, &conn).await?;
            }
            let began = conn.simple(sql).await;
            if began.is_err() {
                restore_query_only(&core, &conn).await;
            }
            drop(conn);
            began.map_err(Failure::from)
        },
        |core, ()| core.enter(),
    )
}

/// Runs one savepoint statement and then adjusts the depth, which it returns. After SQLite ended the
/// transaction itself, a release or rollback has nothing left to do and a new savepoint is refused.
///
/// # Errors
/// `ConnectionBusyError`/`InterfaceError` when the connection cannot be claimed, or the statement's error.
pub fn txn_step<'py>(
    py: Python<'py>,
    core: &Arc<Core>,
    sql: String,
    depth: Depth,
) -> PyResult<Bound<'py, PyAny>> {
    run(
        py,
        core,
        move |core| async move {
            if core.aborted() {
                return match depth {
                    Depth::Enter => Err(Failure::Aborted),
                    Depth::Leave | Depth::Keep => Ok(()),
                };
            }
            let conn = core.conn.lock().await;
            conn.simple(&sql)
                .await
                .map_err(|err| Failure::at(err, &sql))
        },
        move |core, ()| match depth {
            Depth::Enter => core.enter(),
            Depth::Leave => core.leave(),
            Depth::Keep => core.depth(),
        },
    )
}

// SQLite may have ended the transaction already (an interrupted write rolls the whole thing back), in
// which case there is nothing to commit or roll back
async fn finish_transaction(
    core: &Core,
    conn: &SqliteConnection,
    commit: bool,
) -> Result<(), Failure> {
    let open = match conn.is_autocommit().await {
        Ok(autocommit) => !autocommit,
        Err(err) => {
            core.cancel.state.mark_maybe_in_txn();
            return Err(err.into());
        }
    };
    let outcome = if open {
        conn.simple(if commit { "COMMIT" } else { "ROLLBACK" })
            .await
    } else {
        Ok(())
    };
    // a rejected COMMIT (a deferred foreign key, a busy database) leaves the transaction open in SQLite;
    // it ends here, as it does in PostgreSQL, so the caller is never left inside a transaction it gave up
    if commit && matches!(&outcome, Err(Error::Sqlite { .. })) {
        let _ = conn.simple("ROLLBACK").await;
    }
    // only a failure that says nothing about the transaction leaves its outcome open
    if matches!(&outcome, Ok(()) | Err(Error::Sqlite { .. })) {
        core.clear();
    } else {
        core.cancel.state.mark_maybe_in_txn();
    }
    restore_query_only(core, conn).await;
    outcome.map_err(Failure::from)
}

/// Ends the outermost transaction with `COMMIT` or `ROLLBACK`.
///
/// The statement runs in a task of its own, so dropping the caller neither stops it nor interrupts it. A
/// caller that goes away while a `COMMIT` is in flight cannot know the outcome, so the connection is marked
/// broken.
///
/// # Errors
/// `ConnectionBusyError`/`InterfaceError` when the connection cannot be claimed, or the statement's error.
pub fn end_transaction<'py>(
    py: Python<'py>,
    core: &Arc<Core>,
    commit: bool,
) -> PyResult<Bound<'py, PyAny>> {
    let claim = core
        .access
        .claim(&core.cancel.state)
        .map_err(|e| to_py_err(py, &Failure::from(e)))?;
    let core = Arc::clone(core);
    let handle = core.cancel.handle.clone();
    let task = {
        let core = Arc::clone(&core);
        handle.spawn(async move {
            let claim = core
                .access
                .settle_claim(&core.cancel.state, claim)
                .await
                .map_err(Failure::from)?;
            let conn = core.conn.lock().await;
            let result = finish_transaction(&core, &conn, commit).await;
            drop(conn);
            drop(claim);
            result
        })
    };
    let state = Arc::clone(&core.cancel.state);
    let future = future_into_py(py, async move {
        task.await
            .map_err(|e| SqliteError::from(Error::Interface(e.to_string())))?
            .map_err(|failure| SqliteError(fail(&core.cancel.state, failure)))?;
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
