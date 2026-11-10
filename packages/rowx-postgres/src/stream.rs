use std::any::Any;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, OnceLock};

use pyo3::prelude::*;
use pyo3::types::{PyCFunction, PyList};
use pyo3_async_runtimes::tokio::future_into_py;
use rowx_core::error::CoreError;
use rowx_core::row_shape::Mapping;
use rowx_core::state::State;
use rowx_postgres::cancel::{self, Cancel, Hook};
use rowx_postgres::conn::Connection as PgConnection;
use rowx_postgres::error::Error;
use rowx_postgres::exec::{self as pg, Batch};
use rowx_postgres::pool::{Pool as PgPool, Pooled};
use rowx_postgres::{Row, Type};
use tokio::sync::{Notify, mpsc};

use crate::codec::{self, PyParam};
use crate::errors::{PgError, interface_error, to_py_err};
use crate::exec::{
    Access, Layout, decode_row, driver_shape, fail_named, note_sql, nullable_mask, watch,
};
use crate::named::RowShape;

type Msg = Result<Batch, Error>;

#[derive(Debug)]
pub struct StreamArgs {
    pub sql: String,
    // per-column flags, see `FetchArgs`
    pub mask: Option<Vec<u8>>,
    pub scalar: bool,
    pub arity: Option<usize>,
    pub batch_size: usize,
    // a named row type; `mask`, `scalar` and `arity` are then unused
    pub shape: Option<Py<RowShape>>,
}

// how the first `_next` gets its connection
#[derive(Debug)]
enum Starting {
    Claimed(Start),
    Acquire(Start),
}

#[derive(Debug)]
struct Start {
    args: StreamArgs,
    params: Vec<PyParam>,
    tx: mpsc::Sender<Msg>,
}

// state shared between the binding object and the futures it hands out
#[derive(Debug, Default)]
struct Shared {
    finished: AtomicBool,
    // set when the task awaiting `_next` was cancelled; the statement is being cancelled
    cancelled: AtomicBool,
    cancel_wake: Notify,
    stop: Notify,
    hook: Hook,
    rows_affected: Mutex<Option<u64>>,
    // a named row's column-to-field mapping, set by the producer once the statement is prepared and
    // before it sends anything
    mapping: OnceLock<Mapping>,
    // the named layout, built from the first batch and fixed for the stream
    named: OnceLock<Layout>,
    // rows already handed to Python, so a constructor error names its position in the whole stream
    built: AtomicUsize,
    // the error of a batch whose earlier rows were already handed over
    pending: Mutex<Option<PyErr>>,
    // a pool's checkout, held until the stream is over; dropping it releases the connection
    // type-erased: dropping it releases the checkout, and clippy would otherwise treat every
    // `RowStream` as holding a significant drop
    pooled: Mutex<Option<Box<dyn Any + Send>>>,
}

impl Shared {
    // the named layout is built from the first batch and fixed for the stream
    fn named_layout(&self, py: Python<'_>, shape: &Py<RowShape>, first: &Row) -> PyResult<&Layout> {
        if let Some(named) = self.named.get() {
            return Ok(named);
        }
        // a mapping is always published with a named shape; running on without one would silently
        // decode tuples
        let Some(mapping) = self.mapping.get() else {
            return Err(interface_error(
                py,
                "a named row type was given no column mapping",
            ));
        };
        let built = Layout::named(py, shape, mapping, first)?;
        // only the task awaiting `_next` builds it, so the first `set` wins
        let _ = self.named.set(built);
        self.named
            .get()
            .ok_or_else(|| interface_error(py, "the named row layout was not kept"))
    }

    // idempotent and synchronous: the claim is released (Busy → Idle) before this returns
    fn finish(&self) {
        self.finished.store(true, Ordering::Release);
        // a cancelled stream must keep reading until the server confirms, or the CancelRequest
        // would find the producer already gone and the statement would run to completion
        if !self.is_cancelled() {
            self.stop.notify_one();
        }
        self.hook.finish();
        // after the claim, so the release finds the connection no longer claimed; it still waits
        // for a producer that is draining what is left
        let pooled = self.pooled.lock().ok().and_then(|mut slot| slot.take());
        drop(pooled);
    }

    fn hold(&self, pooled: Pooled) {
        if let Ok(mut slot) = self.pooled.lock() {
            *slot = Some(Box::new(pooled));
        }
    }

    fn is_finished(&self) -> bool {
        self.finished.load(Ordering::Acquire)
    }

    fn is_cancelled(&self) -> bool {
        self.cancelled.load(Ordering::Acquire)
    }
}

#[pyclass(frozen, module = "rowx_postgres._core", name = "RowStream")]
#[derive(Debug)]
pub struct RowStream {
    // known from the start for a connection; a pool's stream learns it at its first `_next`
    target: Arc<OnceLock<(Cancel, Access)>>,
    pool: Option<PgPool>,
    layout: Arc<Layout>,
    shape: Option<Py<RowShape>>,
    start: Mutex<Option<Start>>,
    rx: Arc<tokio::sync::Mutex<mpsc::Receiver<Msg>>>,
    shared: Arc<Shared>,
}

impl RowStream {
    /// # Errors
    /// `EncodeError`/`InterfaceError` when a parameter cannot be extracted.
    pub fn new(
        py: Python<'_>,
        cancel: &Cancel,
        access: &Access,
        params: &Bound<'_, PyList>,
        args: StreamArgs,
    ) -> PyResult<Self> {
        let target = Arc::new(OnceLock::new());
        let _ = target.set((cancel.clone(), access.clone()));
        Self::build(py, target, None, params, args)
    }

    /// A stream on a pool: the connection is acquired by the first `_next`.
    ///
    /// # Errors
    /// `EncodeError`/`InterfaceError` when a parameter cannot be extracted.
    pub fn for_pool(
        py: Python<'_>,
        pool: PgPool,
        params: &Bound<'_, PyList>,
        args: StreamArgs,
    ) -> PyResult<Self> {
        Self::build(py, Arc::new(OnceLock::new()), Some(pool), params, args)
    }

    fn build(
        py: Python<'_>,
        target: Arc<OnceLock<(Cancel, Access)>>,
        pool: Option<PgPool>,
        params: &Bound<'_, PyList>,
        args: StreamArgs,
    ) -> PyResult<Self> {
        let params = codec::extract(py, params)?;
        let (tx, rx) = mpsc::channel(1);
        Ok(Self {
            target,
            pool,
            layout: Arc::new(Layout::new(args.scalar, args.mask.as_deref())),
            shape: args.shape.as_ref().map(|shape| shape.clone_ref(py)),
            start: Mutex::new(Some(Start { args, params, tx })),
            rx: Arc::new(tokio::sync::Mutex::new(rx)),
            shared: Arc::new(Shared::default()),
        })
    }
}

impl Drop for RowStream {
    fn drop(&mut self) {
        // no async work, so this is safe outside a runtime
        self.shared.finish();
    }
}

#[pymethods]
impl RowStream {
    #[pyo3(name = "_next")]
    fn next<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let pending = self.shared.pending.lock().ok().and_then(|mut e| e.take());
        if let Some(err) = pending {
            self.shared.finish();
            return Err(err);
        }
        if self.shared.is_finished() || self.shared.is_cancelled() {
            // a cancelled stream is over, so a pool's connection goes back now
            self.shared.finish();
            return future_into_py(py, async { Ok(empty()) });
        }
        let first = self.start.lock().ok().and_then(|mut slot| slot.take());
        let known = self.target.get().cloned();
        // claimed synchronously so a second call made before the first is awaited fails immediately
        let starting = match (first, &known) {
            (Some(start), Some((cancel, access))) => {
                match self.shared.hook.reserve(|| access.claim(&cancel.state)) {
                    Ok(_) => Some(Starting::Claimed(start)),
                    Err(e) => {
                        self.shared.finish();
                        return Err(to_py_err(py, &e.into()));
                    }
                }
            }
            (Some(start), None) => Some(Starting::Acquire(start)),
            (None, _) => None,
        };
        // a later call made while a pool's first call is still acquiring finds no connection yet
        if starting.is_none() && known.is_none() {
            return Err(to_py_err(py, &CoreError::Busy.into()));
        }
        let target = Arc::clone(&self.target);
        let pool = self.pool.clone();
        let rx = Arc::clone(&self.rx);
        let shared = Arc::clone(&self.shared);
        let layout = Arc::clone(&self.layout);
        let shape = self.shape.as_ref().map(|shape| shape.clone_ref(py));
        let future = future_into_py(py, async move {
            if let Some(starting) = starting {
                let (start, cancel, access) = match starting {
                    Starting::Claimed(start) => {
                        let Some((cancel, access)) = target.get().cloned() else {
                            return Err(PgError(Error::Core(CoreError::Closed)).into());
                        };
                        (start, cancel, access)
                    }
                    Starting::Acquire(start) => {
                        let Some(pool) = pool else {
                            return Err(PgError(Error::Core(CoreError::Closed)).into());
                        };
                        let Some((cancel, access)) = acquire(pool, &shared, &target).await? else {
                            return Ok(empty());
                        };
                        (start, cancel, access)
                    }
                };
                if let Err(e) = access.reserve(&cancel.state, &shared.hook).await {
                    if shared.is_finished() {
                        return Ok(empty());
                    }
                    shared.finish();
                    return Err(e.into());
                }
                let inner = Arc::clone(&cancel.backend.conn);
                let state = Arc::clone(&cancel.state);
                let producer = Arc::clone(&shared);
                if let Err(e) = cancel::spawn_stream(
                    &cancel,
                    &shared.hook,
                    produce(inner, state, start, producer),
                ) {
                    if shared.is_finished() {
                        return Ok(empty());
                    }
                    shared.finish();
                    return Err(PgError(e.into()).into());
                }
            }
            let Some((cancel, access)) = target.get().cloned() else {
                return Err(PgError(Error::Core(CoreError::Closed)).into());
            };
            receive(&cancel, &access, &rx, &shared, &layout, shape.as_ref()).await
        })
        .inspect_err(|_| self.shared.finish())?;
        watch(&future, self.shared.hook.clone())?;
        watch_cancelled(&future, Arc::clone(&self.shared))?;
        Ok(future)
    }

    #[getter]
    fn rows_affected(&self) -> Option<u64> {
        self.shared.rows_affected.lock().ok().and_then(|n| *n)
    }

    fn close(&self) {
        self.shared.finish();
    }
}

// takes a connection from the pool for the first `_next` and keeps the checkout in `shared`, so the
// end of the stream releases it; `None` when the stream was closed or cancelled while it waited
async fn acquire(
    pool: PgPool,
    shared: &Shared,
    target: &OnceLock<(Cancel, Access)>,
) -> PyResult<Option<(Cancel, Access)>> {
    match pool.acquire().await {
        Ok(pooled) => adopt(pooled, pool, shared, target),
        Err(e) => {
            shared.finish();
            Err(PgError(e.into()).into())
        }
    }
}

fn adopt(
    pooled: Pooled,
    pool: PgPool,
    shared: &Shared,
    target: &OnceLock<(Cancel, Access)>,
) -> PyResult<Option<(Cancel, Access)>> {
    if shared.is_finished() || shared.is_cancelled() {
        return Ok(None);
    }
    let cancel = pooled.conn().map_err(PgError::from)?.cancel.clone();
    let access = Access {
        lease: Some(pooled.lease()),
        pool: Some(pool),
    };
    let _ = target.set((cancel.clone(), access.clone()));
    shared.hold(pooled);
    Ok(Some((cancel, access)))
}

// registered after `watch`, so the cancel path has started when the flag is seen; a cancelled
// stream is over: later calls return no rows
fn watch_cancelled(future: &Bound<'_, PyAny>, shared: Arc<Shared>) -> PyResult<()> {
    let callback = PyCFunction::new_closure(future.py(), None, None, move |args, _kwargs| {
        if let Ok(done) = args.get_item(0)
            && done.call_method0("cancelled")?.is_truthy()?
        {
            shared.cancelled.store(true, Ordering::Release);
            shared.cancel_wake.notify_one();
        }
        PyResult::Ok(())
    })?;
    future.call_method1("add_done_callback", (callback,))?;
    Ok(())
}

fn empty() -> Py<PyAny> {
    Python::attach(|py| PyList::empty(py).into_any().unbind())
}

async fn receive(
    cancel: &Cancel,
    access: &Access,
    rx: &tokio::sync::Mutex<mpsc::Receiver<Msg>>,
    shared: &Shared,
    layout: &Layout,
    shape: Option<&Py<RowShape>>,
) -> PyResult<Py<PyAny>> {
    if cancel.state.is_closed() {
        shared.finish();
        return Err(crate::errors::PgError(Error::Core(CoreError::Closed)).into());
    }
    let message = rx.lock().await.recv().await;
    if shared.is_cancelled() {
        shared.finish();
        return Ok(empty());
    }
    match message {
        Some(Ok(Batch::Rows(rows))) => Python::attach(|py| {
            let layout = match (shape, rows.first()) {
                (Some(shape), Some(first)) => match shared.named_layout(py, shape, first) {
                    Ok(named) => named,
                    Err(err) => {
                        shared.finish();
                        return Err(err);
                    }
                },
                _ => layout,
            };
            let first_index = shared.built.load(Ordering::Acquire);
            let mut out = Vec::with_capacity(rows.len());
            for (index, row) in rows.iter().enumerate() {
                match decode_row(py, row, layout, first_index + index) {
                    Ok(value) => out.push(value),
                    Err(err) => {
                        shared.finish();
                        // the rows before the failing one are still delivered
                        if index == 0 {
                            return Err(err);
                        }
                        if let Ok(mut slot) = shared.pending.lock() {
                            *slot = Some(err);
                        }
                        break;
                    }
                }
            }
            shared
                .built
                .store(first_index + out.len(), Ordering::Release);
            Ok(PyList::new(py, out)?.into_any().unbind())
        }),
        Some(Ok(Batch::End { rows_affected })) => {
            if let Ok(mut slot) = shared.rows_affected.lock() {
                *slot = rows_affected;
            }
            shared.finish();
            Ok(empty())
        }
        Some(Err(err)) => {
            shared.finish();
            // a connection closed mid-stream cancels the statement, which is not a database failure
            let err = if cancel.state.is_closed() {
                Error::Core(CoreError::Closed)
            } else {
                err
            };
            Err(fail_named(access, &cancel.state, err, shape))
        }
        None => {
            shared.finish();
            // the producer also ends without a message when the connection is closed under it
            if cancel.state.is_closed() {
                return Err(crate::errors::PgError(Error::Core(CoreError::Closed)).into());
            }
            Ok(empty())
        }
    }
}

async fn produce(
    inner: Arc<tokio::sync::Mutex<PgConnection>>,
    state: Arc<State>,
    start: Start,
    shared: Arc<Shared>,
) {
    // the lock is held until the producer ends, so the next operation waits for the drain
    let mut conn = inner.lock().await;
    if shared.is_finished() {
        return;
    }
    note_sql(&state, &start.args.sql);
    run(&mut conn, &state, start, &shared).await;
}

async fn run(conn: &mut PgConnection, state: &State, start: Start, shared: &Shared) {
    let Start { args, params, tx } = start;
    let nullable = args.mask.as_deref().map(nullable_mask);
    let opened = match conn.parts() {
        Ok((client, cache)) => {
            let shape = driver_shape(
                args.shape.as_ref().map(Py::get),
                nullable.as_deref(),
                args.scalar,
                false,
                args.arity,
            );
            pg::stream(
                client,
                cache,
                &args.sql,
                &shape,
                |types: &[Type]| codec::encode_all(&params, types),
                args.batch_size,
            )
            .await
        }
        Err(e) => Err(e),
    };
    let mut batches = match opened {
        Ok(batches) => {
            // the receiver reads it only after the first message, which follows this
            if let Some(mapping) = batches.mapping() {
                let _ = shared.mapping.set(mapping.clone());
            }
            batches
        }
        Err(err) => {
            shared.hook.finish();
            deliver(&tx, Err(err), shared, state).await;
            return;
        }
    };
    loop {
        // the stream is dropped on every way out, so the connection task drains what is left
        let batch = tokio::select! {
            batch = batches.next_batch() => batch,
            () = shared.stop.notified() => return,
            () = tx.closed(), if !shared.is_cancelled() => return,
            () = state.wait_closed() => return,
        };
        // after a cancel nobody reads the stream any more: it is read to its end (the server answers
        // the CancelRequest with an error) and the rows are discarded
        let discard = shared.is_cancelled();
        // a drift the statement caused is reported before the rows that may be mis-decoded
        let batch = conn.settled(batch);
        match batch {
            Ok(rows @ Batch::Rows(_)) => {
                if !discard
                    && !deliver(&tx, Ok(rows), shared, state).await
                    && !shared.is_cancelled()
                {
                    return;
                }
            }
            Ok(end @ Batch::End { .. }) => {
                shared.hook.finish();
                if !discard {
                    deliver(&tx, Ok(end), shared, state).await;
                }
                return;
            }
            Err(err) => {
                shared.hook.finish();
                if !discard {
                    deliver(&tx, Err(err), shared, state).await;
                }
                return;
            }
        }
    }
}

// blocks until Python takes the message, which is the backpressure; false when the stream was stopped
async fn deliver(tx: &mpsc::Sender<Msg>, msg: Msg, shared: &Shared, state: &State) -> bool {
    tokio::select! {
        sent = tx.send(msg) => sent.is_ok(),
        () = shared.stop.notified() => false,
        () = shared.cancel_wake.notified() => false,
        () = state.wait_closed() => false,
    }
}
