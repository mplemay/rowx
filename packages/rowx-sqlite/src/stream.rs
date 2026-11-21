use std::any::Any;
use std::fmt;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, OnceLock};

use pyo3::prelude::*;
use pyo3::types::PyList;
use pyo3_async_runtimes::tokio::future_into_py;
use rowx_core::error::CoreError;
use rowx_py_core::errors::interface_error;
use rowx_py_core::exec::{on_cancel, watch};
use rowx_py_core::row::{Layout, nullable_mask};
use rowx_sqlite::cancel::{self, Hook};
use rowx_sqlite::exec::{Batch, Param, RowBatches, StreamLimits, Streaming};
use rowx_sqlite::pool::{Pool as SqlitePool, Pooled};
use tokio::sync::{Notify, oneshot};

use crate::codec;
use crate::conn::Core;
use crate::errors::{Failure, SqliteError, to_py_err};
use crate::exec::{decode_row, driver_shape, fail_named};
use crate::named::{RowShape, layout_named};

type Opened = Result<RowBatches, Failure>;

#[derive(Debug)]
pub(crate) struct StreamArgs {
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

struct Start {
    args: StreamArgs,
    params: Vec<Param>,
}

// the parameters are not printed: they can hold secrets
impl fmt::Debug for Start {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Start")
            .field("args", &self.args)
            .finish_non_exhaustive()
    }
}

// state shared between the binding object and the futures it hands out
#[derive(Debug, Default)]
struct Shared {
    finished: AtomicBool,
    // set when the task awaiting `_next` was cancelled; the statement is being interrupted
    cancelled: AtomicBool,
    stop: Notify,
    // wakes the producer, which holds the connection until the stream is over
    released: Notify,
    hook: Hook,
    rows_affected: Mutex<Option<u64>>,
    // the named layout, built when the stream opens and fixed for it
    named: OnceLock<Layout>,
    // rows already handed to Python, so a constructor error names its position in the whole stream
    built: AtomicUsize,
    // the error of a batch whose earlier rows were already handed over
    pending: Mutex<Option<PyErr>>,
    // the consumer side of the worker's channel; a `_next` takes it for the length of its wait. Dropping
    // it is what lets a worker that is parked on a full channel end.
    batches: Mutex<Option<RowBatches>>,
    // a pool's checkout, held until the stream is over; dropping it releases the connection. Type-erased so the
    // stream is not treated as holding a significant drop.
    pooled: Mutex<Option<Box<dyn Any + Send>>>,
}

impl Shared {
    // idempotent and synchronous: the claim is released (Busy → Idle) before this returns
    fn finish(&self) {
        self.finished.store(true, Ordering::Release);
        self.stop.notify_one();
        self.released.notify_one();
        self.hook.finish();
        let batches = self.batches.lock().ok().and_then(|mut slot| slot.take());
        drop(batches);
        // after the claim, so the release finds the connection no longer claimed
        let pooled = self.pooled.lock().ok().and_then(|mut slot| slot.take());
        drop(pooled);
    }

    fn hold(&self, pooled: Pooled) {
        if let Ok(mut slot) = self.pooled.lock() {
            *slot = Some(Box::new(pooled));
        }
    }

    // a stream that finished meanwhile does not keep its batches
    fn keep(&self, batches: RowBatches) {
        if self.is_finished() {
            return;
        }
        if let Ok(mut slot) = self.batches.lock() {
            *slot = Some(batches);
        }
    }

    fn is_finished(&self) -> bool {
        self.finished.load(Ordering::Acquire)
    }

    fn is_cancelled(&self) -> bool {
        self.cancelled.load(Ordering::Acquire)
    }
}

#[pyclass(frozen, module = "rowx_sqlite._core", name = "RowStream")]
#[derive(Debug)]
pub struct RowStream {
    // known from the start for a connection; a pool's stream learns it at its first `_next`
    target: Arc<OnceLock<Arc<Core>>>,
    pool: Option<SqlitePool>,
    layout: Arc<Layout>,
    shape: Option<Py<RowShape>>,
    start: Mutex<Option<Start>>,
    shared: Arc<Shared>,
}

impl RowStream {
    /// # Errors
    /// `EncodeError`/`InterfaceError` when a parameter cannot be extracted or there are too many.
    pub(crate) fn new(
        py: Python<'_>,
        core: &Arc<Core>,
        params: &Bound<'_, PyList>,
        args: StreamArgs,
    ) -> PyResult<Self> {
        let params = codec::extract_all(py, params)?;
        core.check_params(py, params.len())?;
        let target = Arc::new(OnceLock::new());
        let _ = target.set(Arc::clone(core));
        Ok(Self::build(py, target, None, params, args))
    }

    /// A stream on a pool: the connection is acquired by the first `_next`.
    ///
    /// # Errors
    /// `EncodeError`/`InterfaceError` when a parameter cannot be extracted.
    pub(crate) fn for_pool(
        py: Python<'_>,
        pool: SqlitePool,
        params: &Bound<'_, PyList>,
        args: StreamArgs,
    ) -> PyResult<Self> {
        let params = codec::extract_all(py, params)?;
        Ok(Self::build(
            py,
            Arc::new(OnceLock::new()),
            Some(pool),
            params,
            args,
        ))
    }

    fn build(
        py: Python<'_>,
        target: Arc<OnceLock<Arc<Core>>>,
        pool: Option<SqlitePool>,
        params: Vec<Param>,
        args: StreamArgs,
    ) -> Self {
        Self {
            target,
            pool,
            layout: Arc::new(Layout::new(args.scalar, args.mask.as_deref())),
            shape: args.shape.as_ref().map(|shape| shape.clone_ref(py)),
            start: Mutex::new(Some(Start { args, params })),
            shared: Arc::new(Shared::default()),
        }
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
            // a cancelled stream is over
            self.shared.finish();
            return future_into_py(py, async { Ok(empty()) });
        }
        let first = self.start.lock().ok().and_then(|mut slot| slot.take());
        // claimed synchronously so a second call made before the first is awaited fails immediately
        let known = self.target.get().cloned();
        let starting = match (first, &known) {
            (Some(start), Some(core)) => {
                let state = &core.cancel.state;
                match self.shared.hook.reserve(|| core.access.claim(state)) {
                    Ok(_) => Some(Starting::Claimed(start)),
                    Err(e) => {
                        self.shared.finish();
                        return Err(to_py_err(py, &Failure::from(e)));
                    }
                }
            }
            (Some(start), None) => Some(Starting::Acquire(start)),
            (None, _) => None,
        };
        // a later call made while a pool's first call is still acquiring finds no connection yet
        if starting.is_none() && known.is_none() {
            return Err(to_py_err(py, &Failure::from(CoreError::Busy)));
        }
        let target = Arc::clone(&self.target);
        let pool = self.pool.clone();
        let shared = Arc::clone(&self.shared);
        let layout = Arc::clone(&self.layout);
        let shape = self.shape.as_ref().map(|shape| shape.clone_ref(py));
        let future = future_into_py(py, async move {
            if let Some(starting) = starting {
                let (start, core) = match starting {
                    Starting::Claimed(start) => {
                        let Some(core) = target.get().cloned() else {
                            return Err(SqliteError::from(CoreError::Closed).into());
                        };
                        (start, core)
                    }
                    Starting::Acquire(start) => {
                        let Some(pool) = pool else {
                            return Err(SqliteError::from(CoreError::Closed).into());
                        };
                        let Some(core) = acquire(pool, &shared, &target).await? else {
                            return Ok(empty());
                        };
                        Python::attach(|py| core.check_params(py, start.params.len()))
                            .inspect_err(|_| shared.finish())?;
                        (start, core)
                    }
                };
                open(&core, &shared, start, shape.as_ref()).await?;
            }
            let Some(core) = target.get().cloned() else {
                return Err(SqliteError::from(CoreError::Closed).into());
            };
            receive(&core, &shared, &layout, shape.as_ref()).await
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

// takes a connection from the pool for the first `_next` and keeps the checkout in `shared`, so the end of the
// stream releases it; `None` when the stream was closed or cancelled while it waited
async fn acquire(
    pool: SqlitePool,
    shared: &Shared,
    target: &OnceLock<Arc<Core>>,
) -> PyResult<Option<Arc<Core>>> {
    match rowx_sqlite::pool::acquire(&pool).await {
        Ok(pooled) => adopt(&pool, pooled, shared, target),
        Err(e) => {
            shared.finish();
            Err(SqliteError::from(e).into())
        }
    }
}

fn adopt(
    pool: &SqlitePool,
    pooled: Pooled,
    shared: &Shared,
    target: &OnceLock<Arc<Core>>,
) -> PyResult<Option<Arc<Core>>> {
    if shared.is_finished() || shared.is_cancelled() {
        return Ok(None);
    }
    let core = Arc::new(Core::pooled(pool, &pooled).map_err(SqliteError::from)?);
    let _ = target.set(Arc::clone(&core));
    shared.hold(pooled);
    Ok(Some(core))
}

// starts the producer and waits for the stream to open, which is where a statement's first failure
// arrives; the batches then wait in `shared` for the receiving calls
async fn open(
    core: &Arc<Core>,
    shared: &Arc<Shared>,
    start: Start,
    shape: Option<&Py<RowShape>>,
) -> PyResult<()> {
    if let Err(e) = core.access.reserve(&core.cancel.state, &shared.hook).await {
        if shared.is_finished() {
            return Ok(());
        }
        shared.finish();
        return Err(SqliteError::from(e).into());
    }
    let (sender, opened) = oneshot::channel();
    let producer = Arc::clone(core);
    if let Err(e) = cancel::spawn_stream(&core.cancel, &shared.hook, || {
        produce(producer, start, sender, Arc::clone(shared))
    }) {
        if shared.is_finished() {
            return Ok(());
        }
        shared.finish();
        return Err(SqliteError::from(e).into());
    }
    let batches = match opened.await {
        Ok(Ok(batches)) => batches,
        Ok(Err(failure)) => {
            shared.finish();
            return Err(fail_named(&core.cancel.state, failure, shape));
        }
        Err(_) => {
            shared.finish();
            return Err(SqliteError::from(CoreError::Closed).into());
        }
    };
    if let Some(shape) = shape {
        // a mapping is always published with a named shape; running on without one would silently
        // decode tuples
        let layout = Python::attach(|py| {
            batches.mapping().map_or_else(
                || {
                    Err(interface_error(
                        py,
                        "a named row type was given no column mapping",
                    ))
                },
                |mapping| layout_named(py, shape.get(), mapping, batches.columns()),
            )
        });
        match layout {
            // only the call that opened the stream builds it
            Ok(layout) => {
                let _ = shared.named.set(layout);
            }
            Err(err) => {
                shared.finish();
                return Err(err);
            }
        }
    }
    shared.keep(batches);
    Ok(())
}

// registered after `watch`, so the cancel path has started when the flag is seen; a cancelled
// stream is over: later calls return no rows. Finishing lets the producer end, which the cancelled
// statement's settling waits for.
fn watch_cancelled(future: &Bound<'_, PyAny>, shared: Arc<Shared>) -> PyResult<()> {
    on_cancel(future, move || {
        shared.cancelled.store(true, Ordering::Release);
        shared.finish();
    })
}

fn empty() -> Py<PyAny> {
    Python::attach(|py| PyList::empty(py).into_any().unbind())
}

async fn receive(
    core: &Core,
    shared: &Shared,
    layout: &Layout,
    shape: Option<&Py<RowShape>>,
) -> PyResult<Py<PyAny>> {
    if core.cancel.state.is_closed() {
        shared.finish();
        return Err(SqliteError::from(CoreError::Closed).into());
    }
    let taken = shared.batches.lock().ok().and_then(|mut slot| slot.take());
    let Some(mut batches) = taken else {
        if shared.is_finished() {
            return Ok(empty());
        }
        // another call is waiting on the stream
        return Err(SqliteError::from(CoreError::Busy).into());
    };
    let message = tokio::select! {
        message = batches.next_batch() => Some(message),
        () = shared.stop.notified() => None,
    };
    if shared.is_cancelled() {
        shared.finish();
        return Ok(empty());
    }
    // stopped while waiting: the stream was closed under this call
    let Some(message) = message else {
        return Ok(empty());
    };
    match message {
        Ok(Batch::Rows(rows)) => {
            shared.keep(batches);
            Python::attach(|py| {
                let layout = shared.named.get().unwrap_or(layout);
                let first_index = shared.built.load(Ordering::Acquire);
                let mut out = Vec::with_capacity(rows.len());
                for (index, row) in rows.iter().enumerate() {
                    match decode_row(py, row, layout, first_index.saturating_add(index)) {
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
                    .store(first_index.saturating_add(out.len()), Ordering::Release);
                Ok(PyList::new(py, out)?.into_any().unbind())
            })
        }
        Ok(Batch::End { rows_affected }) => {
            if let Ok(mut slot) = shared.rows_affected.lock() {
                *slot = rows_affected;
            }
            shared.finish();
            Ok(empty())
        }
        Err(err) => {
            shared.finish();
            // a connection closed mid-stream interrupts the statement, which is not a database failure
            let failure = if core.cancel.state.is_closed() && err.is_interrupt() {
                Failure::from(CoreError::Closed)
            } else {
                Failure::from(err)
            };
            Err(fail_named(&core.cancel.state, failure, shape))
        }
    }
}

// holds the connection from the first read until the consumer has seen the stream end (or it is closed,
// cancelled or dropped), so the connection stays busy while buffered batches are still unread
async fn produce(
    core: Arc<Core>,
    start: Start,
    opened: oneshot::Sender<Opened>,
    shared: Arc<Shared>,
) {
    let conn = core.conn.lock().await;
    if core.aborted() {
        let _ = opened.send(Err(Failure::Aborted));
        return;
    }
    let Start { args, params } = start;
    let nullable = args.mask.as_deref().map(nullable_mask);
    let shape = driver_shape(
        args.shape.as_ref().map(Py::get),
        nullable.as_deref(),
        args.scalar,
        false,
        args.arity,
    );
    let limits = StreamLimits {
        batch_size: args.batch_size,
        ..StreamLimits::default()
    };
    match conn.stream(&args.sql, params, &shape, limits).await {
        Ok(Streaming { batches, done }) => {
            // a caller that stopped waiting drops the batches, which ends the worker
            let _ = opened.send(Ok(batches));
            done.wait().await;
            // `finish` stores a permit, so a stream that ended meanwhile is not waited for
            if !shared.is_finished() {
                tokio::select! {
                    () = shared.released.notified() => {}
                    () = core.cancel.state.wait_closed() => {}
                    // a pool release or a cancelled stream wants the connection back
                    () = core.cancel.backend.cancel_requested() => {}
                }
            }
        }
        Err(err) => {
            let _ = opened.send(Err(Failure::at(err, &args.sql)));
        }
    }
}
