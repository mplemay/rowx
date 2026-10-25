use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

use pyo3::prelude::*;
use pyo3::types::PyList;
use pyo3_async_runtimes::tokio::future_into_py;
use rowx_pg::Type;
use rowx_pg::cancel::{self, Cancel, Hook, StreamHandle};
use rowx_pg::conn::Connection as PgConnection;
use rowx_pg::error::Error;
use rowx_pg::exec::{self as pg, Batch, Shape};
use rowx_pg::state::State;
use tokio::sync::{Notify, mpsc};

use crate::codec::{self, PyParam};
use crate::errors::to_py_err;
use crate::exec::{decode_row, fail, settle_claim, watch};

type Msg = Result<Batch, Error>;

#[derive(Debug)]
pub struct StreamArgs {
    pub sql: String,
    pub mask: Option<Vec<bool>>,
    pub scalar: bool,
    pub arity: Option<usize>,
    pub batch_size: usize,
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
    stop: Notify,
    handle: Mutex<Option<StreamHandle>>,
    rows_affected: Mutex<Option<u64>>,
    // the error of a batch whose earlier rows were already handed over
    pending: Mutex<Option<PyErr>>,
}

impl Shared {
    // idempotent and synchronous: the claim is released (Busy → Idle) before this returns
    fn finish(&self) {
        self.finished.store(true, Ordering::Release);
        self.stop.notify_one();
        if let Some(handle) = self.handle.lock().ok().and_then(|mut slot| slot.take()) {
            handle.release();
        }
    }

    fn is_finished(&self) -> bool {
        self.finished.load(Ordering::Acquire)
    }
}

#[pyclass(frozen, module = "rowx._core", name = "RowStream")]
#[derive(Debug)]
pub struct RowStream {
    cancel: Cancel,
    hook: Hook,
    scalar: bool,
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
        params: &Bound<'_, PyList>,
        args: StreamArgs,
    ) -> PyResult<Self> {
        let params = codec::extract(py, params)?;
        let (tx, rx) = mpsc::channel(1);
        Ok(Self {
            cancel: cancel.clone(),
            hook: Hook::default(),
            scalar: args.scalar,
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
        if self.shared.is_finished() {
            return future_into_py(py, async { Ok(empty()) });
        }
        let first = self.start.lock().ok().and_then(|mut slot| slot.take());
        // claimed synchronously so a second call made before the first is awaited fails immediately
        let starting = match first {
            Some(start) => match self.cancel.state.claim() {
                Ok(claim) => Some((start, claim)),
                Err(e) => {
                    self.shared.finish();
                    return Err(to_py_err(py, &e));
                }
            },
            None => None,
        };
        let cancel = self.cancel.clone();
        let hook = self.hook.clone();
        let rx = Arc::clone(&self.rx);
        let shared = Arc::clone(&self.shared);
        let scalar = self.scalar;
        let future = future_into_py(py, async move {
            if let Some((start, claim)) = starting {
                let claim = settle_claim(&cancel.state, claim).await?;
                if shared.is_finished() {
                    return Ok(empty());
                }
                let inner = Arc::clone(&cancel.conn);
                let state = Arc::clone(&cancel.state);
                let stop = Arc::clone(&shared);
                let handle = cancel::spawn_stream(&cancel, claim, &hook, move |handle| {
                    produce(inner, state, start, stop, handle)
                });
                if let Ok(mut slot) = shared.handle.lock() {
                    *slot = Some(handle);
                }
                // a close() that ran before the handle was stored could not release it
                if shared.is_finished() {
                    shared.finish();
                    return Ok(empty());
                }
            }
            receive(&cancel, &rx, &shared, scalar).await
        })?;
        watch(&future, self.hook.clone())?;
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

fn empty() -> Py<PyAny> {
    Python::attach(|py| PyList::empty(py).into_any().unbind())
}

async fn receive(
    cancel: &Cancel,
    rx: &tokio::sync::Mutex<mpsc::Receiver<Msg>>,
    shared: &Shared,
    scalar: bool,
) -> PyResult<Py<PyAny>> {
    if cancel.state.is_closed() {
        shared.finish();
        return Err(crate::errors::PgError(Error::Closed).into());
    }
    let message = rx.lock().await.recv().await;
    match message {
        Some(Ok(Batch::Rows(rows))) => Python::attach(|py| {
            let mut out = Vec::with_capacity(rows.len());
            for (index, row) in rows.iter().enumerate() {
                match decode_row(py, row, scalar) {
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
                Error::Closed
            } else {
                err
            };
            Err(fail(&cancel.state, err).into())
        }
        None => {
            shared.finish();
            Ok(empty())
        }
    }
}

async fn produce(
    inner: Arc<tokio::sync::Mutex<PgConnection>>,
    state: Arc<State>,
    start: Start,
    shared: Arc<Shared>,
    handle: StreamHandle,
) {
    // the lock is held until the producer ends, so the next operation waits for the drain
    let mut conn = inner.lock().await;
    run(&mut conn, &state, start, &shared, &handle).await;
}

async fn run(
    conn: &mut PgConnection,
    state: &State,
    start: Start,
    shared: &Shared,
    handle: &StreamHandle,
) {
    let Start { args, params, tx } = start;
    let opened = match conn.parts() {
        Ok((client, cache)) => {
            let shape = Shape {
                mask: args.mask.as_deref(),
                scalar: args.scalar,
                limit_one: false,
                arity: args.arity,
            };
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
        Ok(batches) => batches,
        Err(err) => {
            handle.release();
            deliver(&tx, Err(err), shared, state).await;
            return;
        }
    };
    loop {
        // the stream is dropped on every way out, so the connection task drains what is left
        let batch = tokio::select! {
            batch = batches.next_batch() => batch,
            () = shared.stop.notified() => return,
            () = tx.closed() => return,
            () = state.wait_closed() => return,
        };
        match batch {
            Ok(rows @ Batch::Rows(_)) => {
                if !deliver(&tx, Ok(rows), shared, state).await {
                    return;
                }
            }
            Ok(end @ Batch::End { .. }) => {
                handle.release();
                deliver(&tx, Ok(end), shared, state).await;
                return;
            }
            Err(err) => {
                handle.release();
                deliver(&tx, Err(err), shared, state).await;
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
        () = state.wait_closed() => false,
    }
}
