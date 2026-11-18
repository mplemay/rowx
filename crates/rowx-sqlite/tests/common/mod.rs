use std::future::Future;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use rowx_core::row_shape::Shape;
use rowx_sqlite::cancel::{Cancel, Hook, SqliteBackend, spawn_stream};
use rowx_sqlite::conn::{ConnectOptions, Connection};
use rowx_sqlite::dsn::{OpenMode, OpenOptions, Target};
use rowx_sqlite::error::Error;
use rowx_sqlite::exec::{Batch, RowBatches, Streaming};
use rowx_sqlite::exec::{Fetched, Param, StreamLimits};
use tempfile::TempDir;
use tokio::runtime::Handle;
use tokio::sync::oneshot;

pub type TestResult<T = ()> = Result<T, Box<dyn std::error::Error>>;

// a hang guard, never the mechanism a test relies on
const HANG_GUARD: Duration = Duration::from_secs(60);

// a recursive CTE prefix that counts far longer than any test waits
macro_rules! endless_cte {
    () => {
        "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c WHERE n < 1000000000000000) "
    };
}

// a query that runs far longer than any test waits
pub const LONG_QUERY: &str = concat!(endless_cte!(), "SELECT count(*) FROM c");

// rows without end, for a stream whose consumer stops
pub const ENDLESS_ROWS: &str = concat!(endless_cte!(), "SELECT n FROM c");

#[must_use]
pub const fn memory() -> ConnectOptions {
    ConnectOptions::new(OpenOptions::new(Target::Memory, OpenMode::ReadWrite))
}

#[must_use]
pub fn file_options(dir: &TempDir, name: &str, mode: OpenMode) -> ConnectOptions {
    ConnectOptions::new(OpenOptions::new(Target::File(path(dir, name)), mode))
}

#[must_use]
pub fn path(dir: &TempDir, name: &str) -> PathBuf {
    dir.path().join(name)
}

#[must_use]
pub fn file(dir: &TempDir) -> ConnectOptions {
    file_options(dir, "test.db", OpenMode::ReadWriteCreate)
}

/// # Errors
/// When the future does not finish in time.
pub async fn within<F: Future>(fut: F) -> TestResult<F::Output> {
    Ok(tokio::time::timeout(HANG_GUARD, fut).await?)
}

/// Polls an observable condition; the bound only guards against a hang.
///
/// # Errors
/// When the condition does not hold in time.
pub async fn wait_until(condition: impl Fn() -> bool + Send + Sync) -> TestResult {
    let wait = async {
        while !condition() {
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
    };
    within(wait).await
}

/// # Errors
/// When the connection cannot be opened.
pub async fn connect(options: &ConnectOptions) -> TestResult<Connection> {
    Ok(Connection::connect(options).await?)
}

/// The first column of every row as integers.
///
/// # Errors
/// When the query fails or a value is not an integer.
pub async fn ints(conn: &Connection, sql: &str) -> TestResult<Vec<i64>> {
    let fetched = conn.fetch(sql, Vec::new(), &Shape::default()).await?;
    let mut out = Vec::new();
    for row in fetched.rows {
        match row.into_iter().next().flatten() {
            Some(rowx_sqlite::value::Scalar::Integer(int)) => out.push(int),
            other => return Err(format!("not an integer: {other:?}").into()),
        }
    }
    Ok(out)
}

/// A `PRAGMA` that answers one integer.
///
/// # Errors
/// When the query fails.
pub async fn pragma(conn: &Connection, name: &str) -> TestResult<i64> {
    let sql = format!("PRAGMA {name}");
    let rows = ints(conn, &sql).await?;
    rows.first()
        .copied()
        .ok_or_else(|| format!("{name} returned nothing").into())
}

/// # Errors
/// When the query fails.
pub async fn fetch(conn: &Connection, sql: &str) -> Result<Fetched, Error> {
    conn.fetch(sql, Vec::new(), &Shape::default()).await
}

/// # Errors
/// When the statement fails.
pub async fn run(conn: &Connection, sql: &str) -> Result<u64, Error> {
    conn.execute(sql, Vec::<Param>::new()).await
}

#[must_use]
pub const fn limits(batch_size: usize, buffer: usize) -> StreamLimits {
    StreamLimits { batch_size, buffer }
}

/// The error of a SQLite failure: its code name and SQLSTATE.
#[must_use]
pub fn failure(err: &Error) -> Option<(&'static str, &'static str)> {
    match err {
        Error::Sqlite { name, .. } => Some((name, err.sqlstate()?)),
        _ => None,
    }
}

#[must_use]
pub fn cancel_of(conn: &Connection) -> Cancel {
    Cancel {
        state: Arc::clone(conn.state()),
        backend: conn.backend(),
        handle: Handle::current(),
    }
}

#[must_use]
pub fn backend_of(conn: &Connection) -> SqliteBackend {
    conn.backend()
}

/// A stream running in its own task, the way a binding runs one: the producer task holds the claim
/// until the worker is done, and the consumer receives the batches.
#[derive(Debug)]
pub struct Running {
    pub batches: RowBatches,
    pub hook: Hook,
    pub cancel: Cancel,
}

/// # Errors
/// When the stream cannot be opened.
pub async fn start_stream(
    conn: &Arc<Connection>,
    sql: &'static str,
    limits: StreamLimits,
) -> TestResult<Running> {
    let cancel = cancel_of(conn);
    let hook = Hook::new(cancel.state.claim()?);
    let (sender, receiver) = oneshot::channel();
    let connection = Arc::clone(conn);
    spawn_stream(&cancel, &hook, move || async move {
        match connection
            .stream(sql, Vec::new(), &Shape::default(), limits)
            .await
        {
            Ok(Streaming { batches, done }) => {
                let _ = sender.send(Ok(batches));
                done.wait().await;
            }
            Err(err) => {
                let _ = sender.send(Err(err));
            }
        }
    })?;
    let batches = within(receiver).await???;
    Ok(Running {
        batches,
        hook,
        cancel,
    })
}

/// Reads every batch to the end, as (batch sizes, row count).
///
/// # Errors
/// The stream's own error.
pub async fn drain(batches: &mut RowBatches) -> Result<(Vec<usize>, usize), Error> {
    let mut sizes = Vec::new();
    let mut total = 0;
    loop {
        match batches.next_batch().await? {
            Batch::Rows(rows) => {
                sizes.push(rows.len());
                total += rows.len();
            }
            Batch::End { .. } => return Ok((sizes, total)),
        }
    }
}
