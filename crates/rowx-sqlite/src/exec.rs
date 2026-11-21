use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

use rowx_core::error::TaskStopped;
pub use rowx_core::row_shape::Shape;
use rowx_core::row_shape::{FieldSpec, Mapping, Matching, check_nulls, check_shape, column_mask};
pub use rusqlite::types::Value as Param;
use rusqlite::types::ValueRef;
use rusqlite::{Rows, Statement, ffi, params_from_iter};
use tokio::sync::{mpsc, oneshot};

use crate::cancel::{Shared, Unsent};
use crate::conn::{Connection, Ctx};
use crate::error::Error;
use crate::timeout::{Armed, Timer};
use crate::types::{ColumnType, lookup};
use crate::value::{DecodeError, Scalar, decode};

/// One decoded row; `None` is NULL.
pub type Row = Vec<Option<Scalar>>;

/// A [`Shape`] the worker thread can own.
#[derive(Debug, Clone, Default)]
pub(crate) struct OwnedShape {
    mask: Option<Vec<bool>>,
    scalar: bool,
    limit_one: bool,
    arity: Option<usize>,
    fields: Option<Vec<FieldSpec>>,
    matching: Matching,
}

impl From<&Shape<'_>> for OwnedShape {
    fn from(shape: &Shape<'_>) -> Self {
        Self {
            mask: shape.mask.map(<[bool]>::to_vec),
            scalar: shape.scalar,
            limit_one: shape.limit_one,
            arity: shape.arity,
            fields: shape.fields.map(<[FieldSpec]>::to_vec),
            matching: shape.matching,
        }
    }
}

impl OwnedShape {
    fn as_shape(&self) -> Shape<'_> {
        Shape {
            mask: self.mask.as_deref(),
            scalar: self.scalar,
            limit_one: self.limit_one,
            arity: self.arity,
            fields: self.fields.as_deref(),
            matching: self.matching,
        }
    }
}

#[must_use]
pub fn interrupted() -> Error {
    Error::from_code(ffi::SQLITE_INTERRUPT, "interrupted", None)
}

// The names and types of a statement's columns. They are read again after the first step, because
// SQLite re-prepares a stale statement there and the result columns may have changed.
#[derive(Debug, Clone, PartialEq)]
struct ColumnInfo {
    names: Vec<String>,
    types: Vec<ColumnType>,
}

impl ColumnInfo {
    fn of(stmt: &Statement<'_>) -> Self {
        let columns = stmt.columns();
        Self {
            names: columns.iter().map(|c| c.name().to_owned()).collect(),
            types: columns.iter().map(|c| lookup(c.decl_type())).collect(),
        }
    }
}

// shape validation and row decoding for one result
struct Reader<'a> {
    shape: &'a OwnedShape,
    info: ColumnInfo,
    mapping: Option<Mapping>,
    mask: Option<Vec<bool>>,
    // rows handed out so far, so a NULL error can name its row
    delivered: usize,
}

impl<'a> Reader<'a> {
    fn new(shape: &'a OwnedShape, stmt: &Statement<'_>, delivered: usize) -> Result<Self, Error> {
        let info = ColumnInfo::of(stmt);
        let names: Vec<&str> = info.names.iter().map(String::as_str).collect();
        let mapping = check_shape(&shape.as_shape(), &names)?;
        let mask = match &mapping {
            Some(mapping) => Some(column_mask(&shape.as_shape(), mapping)?),
            None => shape.mask.clone(),
        };
        Ok(Self {
            shape,
            info,
            mapping,
            mask,
            delivered,
        })
    }

    fn refresh(&mut self, stmt: &Statement<'_>) -> Result<(), Error> {
        if ColumnInfo::of(stmt) != self.info {
            *self = Self::new(self.shape, stmt, self.delivered)?;
        }
        Ok(())
    }

    fn read(&mut self, row: &rusqlite::Row<'_>) -> Result<Row, Error> {
        if let Some(mask) = &self.mask {
            check_nulls(
                &self.info.names,
                self.delivered,
                mask,
                self.mapping.as_ref(),
                |index| {
                    row.get_ref(index)
                        .map(|value| matches!(value, ValueRef::Null))
                        .map_err(Error::from)
                },
            )?;
        }
        let mut out = Vec::with_capacity(self.info.types.len());
        for (index, (ty, name)) in self.info.types.iter().zip(&self.info.names).enumerate() {
            let value = row.get_ref(index)?;
            out.push(decode(ty.kind(), value).map_err(|err| {
                Error::Decode(DecodeError::new(format!(
                    "column {index} ({name:?}): {err}"
                )))
            })?);
        }
        self.delivered += 1;
        Ok(out)
    }
}

// The rows of one statement, read on the worker thread. Fields drop in order: the cursor first, which
// resets the statement, then the timer guard, so a timeout can never fire into a statement that is
// still active.
struct RowSource<'a> {
    rows: Rows<'a>,
    armed: Option<Armed>,
    reader: Reader<'a>,
    shared: &'a Shared,
    timer: Option<&'a Timer>,
    conn: &'a rusqlite::Connection,
    readonly: bool,
    first: bool,
}

impl RowSource<'_> {
    // starts the statement-timeout clock; the old guard drops after the new generation took over
    fn arm(&mut self) {
        self.armed = self.timer.map(Timer::arm);
    }

    // stops the clock and reports whether it fired meanwhile
    fn disarm(&mut self) -> bool {
        self.armed.take().is_some_and(Armed::finish)
    }

    const fn mapping(&self) -> Option<&Mapping> {
        self.reader.mapping.as_ref()
    }

    fn columns(&self) -> &[String] {
        &self.reader.info.names
    }

    // `INSERT ... RETURNING` is not read-only; a SELECT has no count of its own
    fn rows_affected(&self) -> Option<u64> {
        (!self.readonly).then(|| self.conn.changes())
    }

    // the worker checks the cancel flag before every step, so a cancel that landed before the
    // statement started stepping is honoured
    fn next_row(&mut self) -> Result<Option<Row>, Error> {
        if self.shared.is_cancelled() {
            return Err(interrupted());
        }
        let first = std::mem::replace(&mut self.first, false);
        let row = if let Some(row) = self.rows.next()? {
            if first {
                self.reader.refresh(row.as_ref())?;
            }
            Some(self.reader.read(row)?)
        } else {
            if first && let Some(stmt) = self.rows.as_ref() {
                self.reader.refresh(stmt)?;
            }
            None
        };
        #[cfg(feature = "test-support")]
        if row.is_some() {
            self.shared.count_row();
        }
        Ok(row)
    }
}

// Prepares through the cache and checks the shape before anything runs, so a statement that returns no
// rows still reports a wrong shape. A cached statement can be stale (SQLite re-prepares it at its first
// step, which is too late for this check), so a failed check retries once on a fresh statement.
fn prepare_checked<'c, 's>(
    conn: &'c rusqlite::Connection,
    sql: &str,
    shape: &'s OwnedShape,
) -> Result<(rusqlite::CachedStatement<'c>, Reader<'s>), Error> {
    let stmt = conn.prepare_cached(sql)?;
    if let Ok(reader) = Reader::new(shape, &stmt, 0) {
        return Ok((stmt, reader));
    }
    drop(stmt);
    conn.flush_prepared_statement_cache();
    refresh_schema(conn)?;
    let stmt = conn.prepare_cached(sql)?;
    let reader = Reader::new(shape, &stmt, 0)?;
    Ok((stmt, reader))
}

// `prepare` reads the schema the connection has in memory, which another connection's DDL leaves
// stale until a statement starts a read transaction and notices the changed schema cookie
pub(crate) fn refresh_schema(conn: &rusqlite::Connection) -> Result<(), Error> {
    conn.prepare("SELECT 1 FROM sqlite_schema LIMIT 0")?
        .query([])?
        .next()?;
    Ok(())
}

fn with_query<R>(
    ctx: &Ctx,
    sql: &str,
    params: Vec<Param>,
    shape: &OwnedShape,
    run: impl FnOnce(&mut RowSource<'_>) -> Result<R, Error>,
) -> Result<R, Error> {
    if ctx.shared.is_cancelled() {
        return Err(interrupted());
    }
    let (mut stmt, reader) = prepare_checked(&ctx.conn, sql, shape)?;
    let readonly = stmt.readonly();
    let rows = stmt.query(params_from_iter(params))?;
    let mut source = RowSource {
        rows,
        armed: None,
        reader,
        shared: &ctx.shared,
        timer: ctx.timer.as_ref(),
        conn: &ctx.conn,
        readonly,
        first: true,
    };
    let out = run(&mut source);
    drop(source);
    out
}

/// Runs a statement that returns no rows and reports the rows it changed.
pub(crate) fn execute_sql(ctx: &Ctx, sql: &str, params: &[Param]) -> Result<u64, Error> {
    if ctx.shared.is_cancelled() {
        return Err(interrupted());
    }
    // declared before the statement, so it drops after the statement was reset
    let armed;
    let mut stmt = ctx.conn.prepare_cached(sql)?;
    // checked before anything steps: `Statement::execute` applies a write before it notices the rows,
    // so `INSERT ... RETURNING` would be committed and then reported as an error
    if stmt.column_count() > 0 {
        return Err(Error::Interface(
            "the statement returns rows; use fetch or stream".to_owned(),
        ));
    }
    if params.len() != stmt.parameter_count() {
        return Err(
            rusqlite::Error::InvalidParameterCount(params.len(), stmt.parameter_count()).into(),
        );
    }
    for (index, param) in params.iter().enumerate() {
        stmt.raw_bind_parameter(index + 1, param)?;
    }
    // The clock starts right before the step: `sqlite3_interrupt` with no statement running is a no-op
    // that the first step clears, so an interrupt fired during prepare or bind would be lost.
    armed = ctx.timer.as_ref().map(Timer::arm);
    let total = ctx.conn.total_changes();
    let changed = stmt.raw_execute();
    drop(armed);
    let changed = changed?;
    // `sqlite3_changes` keeps the count of the last INSERT, UPDATE or DELETE, so a statement that changed
    // nothing (DDL, a `DELETE` that matched no row) must not report a stale one
    let changed = if ctx.conn.total_changes() == total {
        0
    } else {
        changed
    };
    u64::try_from(changed).map_err(|_| Error::Interface("negative change count".to_owned()))
}

/// The rows of a `fetch`, and the column-to-field mapping when the shape is named.
#[derive(Debug)]
pub struct Fetched {
    pub rows: Vec<Row>,
    pub mapping: Option<Mapping>,
    /// The raw column names.
    pub columns: Vec<String>,
}

fn fetch_sql(
    ctx: &Ctx,
    sql: &str,
    params: Vec<Param>,
    shape: &OwnedShape,
) -> Result<Fetched, Error> {
    with_query(ctx, sql, params, shape, |source| {
        source.arm();
        let mut rows = Vec::new();
        while let Some(row) = source.next_row()? {
            rows.push(row);
            if shape.limit_one {
                // the cursor resets with the statement when the source drops
                break;
            }
        }
        source.disarm();
        Ok(Fetched {
            rows,
            mapping: source.mapping().cloned(),
            columns: source.columns().to_vec(),
        })
    })
}

/// How a stream is paced: rows per batch and batches the channel holds.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct StreamLimits {
    pub batch_size: usize,
    /// The worker steps at most `(buffer + 1) * batch_size` rows ahead of the consumer.
    pub buffer: usize,
}

impl Default for StreamLimits {
    fn default() -> Self {
        Self {
            batch_size: 100,
            buffer: 2,
        }
    }
}

/// One trip of a stream.
#[derive(Debug)]
pub enum Batch {
    /// At least one row, at most `batch_size`.
    Rows(Vec<Row>),
    /// The stream is over. `rows_affected` is `None` for a read-only statement or after an error.
    End { rows_affected: Option<u64> },
}

type Item = Result<Batch, Error>;

struct Opened {
    mapping: Option<Mapping>,
    columns: Vec<String>,
}

/// The consumer side of a stream; dropping it stops the worker and resets the statement.
#[derive(Debug)]
pub struct RowBatches {
    rx: mpsc::Receiver<Item>,
    shared: Arc<Shared>,
    // set by the worker when a cancel ended the stream while the channel was full
    cut: Arc<AtomicBool>,
    mapping: Option<Mapping>,
    columns: Vec<String>,
    // set once the stream ended, by its end or by an error
    ended: bool,
    rows_affected: Option<u64>,
}

impl RowBatches {
    /// The column-to-field mapping of a named shape; fixed for the whole stream.
    #[must_use]
    pub const fn mapping(&self) -> Option<&Mapping> {
        self.mapping.as_ref()
    }

    /// The raw column names.
    #[must_use]
    pub fn columns(&self) -> &[String] {
        &self.columns
    }

    /// The next batch. A failure after some rows were read comes after them, in the next call.
    ///
    /// # Errors
    /// `Decode`/`NullField`/`Sqlite` for a failure while reading (a cancel that cut the stream is
    /// `SQLITE_INTERRUPT`), `Stopped` when the worker died without ending the stream.
    pub async fn next_batch(&mut self) -> Result<Batch, Error> {
        if self.ended {
            return Ok(Batch::End {
                rows_affected: self.rows_affected,
            });
        }
        let item = self.rx.recv().await;
        // a slot is free, so a worker parked on a full channel can go on
        self.shared.wake();
        match item {
            Some(Ok(Batch::End { rows_affected })) => {
                self.ended = true;
                self.rows_affected = rows_affected;
                Ok(Batch::End { rows_affected })
            }
            Some(Ok(rows)) => Ok(rows),
            Some(Err(err)) => {
                self.ended = true;
                Err(err)
            }
            None => {
                self.ended = true;
                if self.cut.load(Ordering::Acquire) {
                    Err(interrupted())
                } else {
                    Err(TaskStopped.into())
                }
            }
        }
    }
}

impl Drop for RowBatches {
    fn drop(&mut self) {
        // closed first, so the worker's next `try_send` fails, then woken in case it is parked
        self.rx.close();
        self.shared.wake();
    }
}

/// Resolves once the stream's worker finished: its statement is reset and the transaction state is
/// reconciled, so the connection is quiescent. A stream task awaits this and never detaches.
#[derive(Debug)]
pub struct Done(oneshot::Receiver<()>);

impl Done {
    /// A worker that died also resolves this.
    pub async fn wait(self) {
        let _ = self.0.await;
    }
}

/// An open stream: the batches, and the completion handle for the task that owns the claim.
#[derive(Debug)]
pub struct Streaming {
    pub batches: RowBatches,
    pub done: Done,
}

struct StreamJob {
    sql: String,
    params: Vec<Param>,
    shape: OwnedShape,
    batch_size: usize,
    tx: mpsc::Sender<Item>,
    cut: Arc<AtomicBool>,
    open: oneshot::Sender<Result<Opened, Error>>,
    done: oneshot::Sender<()>,
}

// Whether the worker should keep going after a send. A cancel that ends a send is recorded for the
// receiver, which otherwise could not tell it from a dead worker.
fn deliver(shared: &Shared, tx: &mpsc::Sender<Item>, cut: &AtomicBool, item: Item) -> bool {
    match shared.send(tx, item) {
        Ok(()) => true,
        Err(Unsent::Cancelled) => {
            cut.store(true, Ordering::Release);
            false
        }
        Err(Unsent::Closed) => false,
    }
}

fn run_stream(
    source: &mut RowSource<'_>,
    batch_size: usize,
    tx: &mpsc::Sender<Item>,
    cut: &AtomicBool,
    open: &mut Option<oneshot::Sender<Result<Opened, Error>>>,
) -> Result<(), Error> {
    // the first step runs before the open reply: it is where a stale statement is re-prepared and
    // where a statement's first failure arrives, so those are errors of the open
    source.arm();
    let first = source.next_row()?;
    if source.disarm() {
        return Err(interrupted());
    }
    let opened = Opened {
        mapping: source.mapping().cloned(),
        columns: source.columns().to_vec(),
    };
    if let Some(reply) = open.take()
        && reply.send(Ok(opened)).is_err()
    {
        // the caller stopped waiting for the stream
        return Ok(());
    }
    let mut ended = first.is_none();
    let mut batch: Vec<Row> = first.into_iter().collect();
    loop {
        // the clock runs while rows are read, not while the worker waits for the consumer
        source.arm();
        let mut failure = None;
        while !ended && batch.len() < batch_size {
            match source.next_row() {
                Ok(Some(row)) => batch.push(row),
                Ok(None) => ended = true,
                Err(err) => {
                    failure = Some(err);
                    break;
                }
            }
        }
        if source.disarm() && failure.is_none() && !ended {
            failure = Some(interrupted());
        }
        // the rows before a failure go out first
        if !batch.is_empty()
            && !deliver(
                source.shared,
                tx,
                cut,
                Ok(Batch::Rows(std::mem::take(&mut batch))),
            )
        {
            return Ok(());
        }
        if let Some(err) = failure {
            return Err(err);
        }
        if ended {
            let end = Batch::End {
                rows_affected: source.rows_affected(),
            };
            deliver(source.shared, tx, cut, Ok(end));
            return Ok(());
        }
    }
}

fn stream_job(ctx: &Ctx, job: StreamJob) {
    let StreamJob {
        sql,
        params,
        shape,
        batch_size,
        tx,
        cut,
        open,
        done,
    } = job;
    let mut open = Some(open);
    let outcome = with_query(ctx, &sql, params, &shape, |source| {
        run_stream(source, batch_size, &tx, &cut, &mut open)
    });
    if let Err(err) = outcome {
        match open.take() {
            Some(reply) => {
                let _ = reply.send(Err(err));
            }
            None => {
                deliver(&ctx.shared, &tx, &cut, Err(err));
            }
        }
    }
    // the statement was reset when `with_query` returned
    ctx.reconcile();
    let _ = done.send(());
}

impl Connection {
    /// Runs one statement that returns no rows and returns the rows it changed.
    ///
    /// # Errors
    /// `Sqlite` for a failure SQLite reports, `Interface` for a statement that returns rows.
    pub async fn execute(&self, sql: &str, params: Vec<Param>) -> Result<u64, Error> {
        let sql = sql.to_owned();
        self.call(move |ctx| execute_sql(ctx, &sql, &params)).await
    }

    /// Runs a query and collects its rows.
    ///
    /// # Errors
    /// `RowShape` for an arity, scalar or NULL-mask violation, `Shape` for a mapping failure,
    /// `NullField` for a NULL in a non-nullable mapped field, `Decode` for a stored value that
    /// disagrees with its declared type, `Sqlite` for a failure SQLite reports.
    pub async fn fetch(
        &self,
        sql: &str,
        params: Vec<Param>,
        shape: &Shape<'_>,
    ) -> Result<Fetched, Error> {
        let (sql, shape) = (sql.to_owned(), OwnedShape::from(shape));
        self.call(move |ctx| fetch_sql(ctx, &sql, params, &shape))
            .await
    }

    /// Opens a query as batches. The worker reads ahead by at most `buffer` batches and parks when
    /// the channel is full. Everything `fetch` reports before the first row is reported here; later
    /// failures come from `next_batch`.
    ///
    /// # Errors
    /// As `fetch`, for what happens before the first row.
    pub async fn stream(
        &self,
        sql: &str,
        params: Vec<Param>,
        shape: &Shape<'_>,
        limits: StreamLimits,
    ) -> Result<Streaming, Error> {
        let (tx, rx) = mpsc::channel(limits.buffer.max(1));
        let (open, opened) = oneshot::channel();
        let (done, finished) = oneshot::channel();
        let cut = Arc::new(AtomicBool::new(false));
        let job = StreamJob {
            sql: sql.to_owned(),
            params,
            shape: OwnedShape::from(shape),
            batch_size: limits.batch_size.max(1),
            tx,
            cut: Arc::clone(&cut),
            open,
            done,
        };
        self.submit(Box::new(move |ctx| stream_job(ctx, job)))?;
        let opened = opened.await.map_err(|_| TaskStopped)??;
        Ok(Streaming {
            batches: RowBatches {
                rx,
                shared: Arc::clone(self.shared()),
                cut,
                mapping: opened.mapping,
                columns: opened.columns,
                ended: false,
                rows_affected: None,
            },
            done: Done(finished),
        })
    }

    /// Runs a multi-statement script for migrations. Runtime multi-statement execution is a non-goal
    /// (#300), so this is separate from `execute`. The statement timeout does not apply: a migration
    /// is expected to run long, and a cancel still interrupts it.
    ///
    /// # Errors
    /// `Sqlite` for the first failing statement.
    pub async fn execute_migration_script(&self, sql: &str) -> Result<(), Error> {
        let sql = sql.to_owned();
        self.call(move |ctx| {
            if ctx.shared.is_cancelled() {
                return Err(interrupted());
            }
            ctx.conn.execute_batch(&sql)?;
            Ok(())
        })
        .await
    }
}
