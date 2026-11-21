use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

use rowx_core::cancel::Backend;
use rowx_core::pool::{Manage, PoolError};
use rowx_core::state::{Phase, State};
use tokio::sync::{Mutex, Notify};

use crate::cancel::SqliteBackend;
use crate::conn::{ConnectOptions, Connection};
use crate::dsn::Target;
use crate::error::Error;

pub type Pool = rowx_core::pool::Pool<SqliteManager>;
pub type Pooled = rowx_core::pool::Pooled<SqliteManager>;
pub use rowx_core::pool::PoolOptions;

const GONE: &str = "the in-memory database is gone: its connection was lost and a new one would be an empty database, so the pool is permanently broken; create a new pool";

/// A pooled connection: the connection behind a lock, plus what the pool and a call need without taking it.
#[derive(Debug, Clone)]
pub struct SqliteConn {
    conn: Arc<Mutex<Connection>>,
    state: Arc<State>,
    backend: SqliteBackend,
    variable_limit: usize,
    // a read-only connection cannot take the default `BEGIN IMMEDIATE`
    read_only: bool,
}

impl SqliteConn {
    #[must_use]
    pub fn new(conn: Connection, read_only: bool) -> Self {
        Self {
            state: Arc::clone(conn.state()),
            backend: conn.backend(),
            variable_limit: conn.variable_limit(),
            read_only,
            conn: Arc::new(Mutex::new(conn)),
        }
    }

    #[must_use]
    pub const fn conn(&self) -> &Arc<Mutex<Connection>> {
        &self.conn
    }

    #[must_use]
    pub const fn state(&self) -> &Arc<State> {
        &self.state
    }

    #[must_use]
    pub fn backend(&self) -> SqliteBackend {
        self.backend.clone()
    }

    #[must_use]
    pub const fn variable_limit(&self) -> usize {
        self.variable_limit
    }

    #[must_use]
    pub const fn read_only(&self) -> bool {
        self.read_only
    }
}

/// Takes a connection, or fails at once when a `:memory:` pool has lost its database, because waiting for a
/// connection that can never be made would only end in a timeout.
///
/// # Errors
/// The pool's own failure (a timeout, a closed pool), a failed connect, or the lost database of a `:memory:` pool.
pub async fn acquire(pool: &Pool) -> Result<Pooled, Error> {
    let acquiring = pool.acquire();
    tokio::pin!(acquiring);
    tokio::select! {
        pooled = &mut acquiring => pooled.map_err(Error::from),
        err = pool.manager().wait_gone() => Err(err),
    }
}

impl From<PoolError<Self>> for Error {
    fn from(err: PoolError<Self>) -> Self {
        match err {
            PoolError::Core(core) => Self::Core(core),
            PoolError::Manage(err) => err,
            PoolError::ConnectTimeout(err) => Self::Interface(format!(
                "timed out waiting for a pooled connection; the last connect failed: {err}"
            )),
        }
    }
}

/// Makes and maintains the SQLite connections of a pool.
///
/// A `:memory:` database lives and dies with its one connection, so that manager connects once and refuses every
/// later connect: a refill would hand out an empty database in place of the lost one.
#[derive(Debug)]
pub struct SqliteManager {
    options: ConnectOptions,
    memory: bool,
    opened: AtomicBool,
    refused: AtomicBool,
    wake: Notify,
}

impl SqliteManager {
    #[must_use]
    pub fn new(options: ConnectOptions) -> Self {
        Self {
            memory: matches!(options.open.target, Target::Memory),
            options,
            opened: AtomicBool::new(false),
            refused: AtomicBool::new(false),
            wake: Notify::new(),
        }
    }

    /// The error of a lost `:memory:` database, once a connect was refused.
    fn gone(&self) -> Option<Error> {
        self.refused
            .load(Ordering::Acquire)
            .then(|| Error::Interface(GONE.to_owned()))
    }

    /// Resolves once a connect was refused, so a caller waiting for a connection can stop waiting.
    async fn wait_gone(&self) -> Error {
        loop {
            let notified = self.wake.notified();
            tokio::pin!(notified);
            notified.as_mut().enable();
            if let Some(err) = self.gone() {
                return err;
            }
            notified.await;
        }
    }
}

impl Manage for SqliteManager {
    type Conn = SqliteConn;
    type Error = Error;
    type Backend = SqliteBackend;

    async fn connect(&self) -> Result<SqliteConn, Error> {
        if self.memory && self.opened.load(Ordering::Acquire) {
            self.refused.store(true, Ordering::Release);
            self.wake.notify_waiters();
            return Err(Error::Interface(GONE.to_owned()));
        }
        let conn = Connection::connect(&self.options).await?;
        // set after the open, so a connect that failed or was cancelled can be tried again
        if self.memory {
            self.opened.store(true, Ordering::Release);
        }
        Ok(SqliteConn::new(conn, self.options.is_read_only()))
    }

    fn state<'a>(&self, conn: &'a SqliteConn) -> &'a Arc<State> {
        &conn.state
    }

    fn backend(&self, conn: &SqliteConn) -> SqliteBackend {
        conn.backend()
    }

    async fn ping(&self, conn: &SqliteConn) -> Result<(), Error> {
        conn.conn.lock().await.ping().await
    }

    // The flag says the state saw a transaction begin; SQLite knows whether one is still open (an interrupt may
    // have rolled it back already, and a `ROLLBACK` outside one is an error), so it is asked. The `query_only`
    // a read-only transaction turned on is put back as well.
    async fn reset(&self, conn: &SqliteConn, _rollback: bool) -> Result<(), Error> {
        let writable = !self.options.is_read_only();
        let guard = conn.conn.lock().await;
        guard
            .call(move |ctx| {
                if !ctx.conn.is_autocommit() {
                    ctx.conn.execute_batch("ROLLBACK")?;
                }
                if writable {
                    ctx.conn.pragma_update(None, "query_only", false)?;
                }
                Ok(())
            })
            .await
    }

    fn is_broken(&self, conn: &SqliteConn) -> bool {
        if matches!(conn.state.phase(), Phase::Broken | Phase::Closed) {
            return true;
        }
        // a locked connection is in use, so it is not dead
        conn.conn.try_lock().is_ok_and(|c| c.is_broken())
    }

    async fn settled(&self, conn: &SqliteConn) -> bool {
        conn.state.wait_settled().await.is_ok()
    }

    async fn forget_statements(&self, conn: &SqliteConn) {
        // a connection that cannot answer has nothing to forget
        let _ = conn.conn.lock().await.forget_statements().await;
    }

    async fn close(&self, conn: SqliteConn) {
        // a locked connection has an operation running, which would hold the lock for its full length
        let running = conn.conn.try_lock().is_err();
        conn.state.close();
        if running {
            // a failed request is fine because closing ends the statement anyway
            let _ = conn.backend.send_cancel().await;
        }
        conn.conn.lock().await.close().await;
    }
}

#[cfg(test)]
mod tests {
    use rowx_core::pool::Pool as CorePool;
    use tempfile::TempDir;

    use super::*;
    use crate::dsn::{OpenMode, OpenOptions, Target};

    fn memory() -> ConnectOptions {
        ConnectOptions::new(OpenOptions::new(Target::Memory, OpenMode::ReadWriteCreate))
    }

    fn file(dir: &TempDir, name: &str) -> ConnectOptions {
        ConnectOptions::new(OpenOptions::new(
            Target::File(dir.path().join(name)),
            OpenMode::ReadWriteCreate,
        ))
    }

    #[tokio::test]
    async fn a_memory_manager_connects_once_and_refuses_the_next_connect() {
        let manager = SqliteManager::new(memory());
        assert!(manager.connect().await.is_ok());
        assert!(manager.gone().is_none());
        let refused = manager.connect().await.err();
        assert!(
            matches!(&refused, Some(Error::Interface(text)) if text.contains("permanently broken")),
            "{refused:?}"
        );
        assert!(manager.gone().is_some());
        assert!(manager.connect().await.is_err());
        assert!(matches!(manager.wait_gone().await, Error::Interface(_)));
    }

    #[tokio::test]
    async fn a_file_manager_connects_again() {
        let dir = TempDir::new();
        let Ok(dir) = dir else { return };
        let manager = SqliteManager::new(file(&dir, "a.db"));
        assert!(manager.connect().await.is_ok());
        assert!(manager.connect().await.is_ok());
        assert!(manager.gone().is_none());
    }

    #[tokio::test]
    async fn reset_rolls_back_only_an_open_transaction() -> Result<(), Box<dyn std::error::Error>> {
        let dir = TempDir::new()?;
        let manager = SqliteManager::new(file(&dir, "r.db"));
        let conn = manager.connect().await?;
        // nothing is open, and a blind ROLLBACK would be an error
        manager.reset(&conn, true).await?;
        manager.reset(&conn, false).await?;
        conn.conn.lock().await.simple("BEGIN IMMEDIATE").await?;
        assert!(!conn.conn.lock().await.is_autocommit().await?);
        manager.reset(&conn, true).await?;
        assert!(conn.conn.lock().await.is_autocommit().await?);
        Ok(())
    }

    #[tokio::test]
    async fn reset_after_an_interrupted_transaction_does_not_roll_back_twice()
    -> Result<(), Box<dyn std::error::Error>> {
        let dir = TempDir::new()?;
        let manager = SqliteManager::new(file(&dir, "i.db"));
        let conn = manager.connect().await?;
        let guard = conn.conn.lock().await;
        // the raw handle rolls back on its own, as an interrupted write does
        guard
            .run_raw(|raw| raw.execute_batch("BEGIN IMMEDIATE; ROLLBACK;"))
            .await??;
        drop(guard);
        manager.reset(&conn, true).await?;
        Ok(())
    }

    #[tokio::test]
    async fn reset_puts_query_only_back() -> Result<(), Box<dyn std::error::Error>> {
        let dir = TempDir::new()?;
        let manager = SqliteManager::new(file(&dir, "q.db"));
        let conn = manager.connect().await?;
        conn.conn
            .lock()
            .await
            .simple("PRAGMA query_only = ON")
            .await?;
        manager.reset(&conn, false).await?;
        let on: bool = conn
            .conn
            .lock()
            .await
            .run_raw(|raw| raw.pragma_query_value(None, "query_only", |row| row.get(0)))
            .await??;
        assert!(!on);
        Ok(())
    }

    #[tokio::test]
    async fn reset_leaves_query_only_alone_on_a_read_only_open()
    -> Result<(), Box<dyn std::error::Error>> {
        let dir = TempDir::new()?;
        // the file has to exist before it can be opened read-only
        let seed = SqliteManager::new(file(&dir, "o.db"));
        let first = seed.connect().await?;
        seed.close(first).await;
        let manager = SqliteManager::new(ConnectOptions::new(OpenOptions::new(
            Target::File(dir.path().join("o.db")),
            OpenMode::ReadOnly,
        )));
        let conn = manager.connect().await?;
        conn.conn
            .lock()
            .await
            .simple("PRAGMA query_only = ON")
            .await?;
        manager.reset(&conn, false).await?;
        let on: bool = conn
            .conn
            .lock()
            .await
            .run_raw(|raw| raw.pragma_query_value(None, "query_only", |row| row.get(0)))
            .await??;
        assert!(on);
        Ok(())
    }

    #[tokio::test]
    async fn a_closed_connection_is_broken() -> Result<(), Box<dyn std::error::Error>> {
        let dir = TempDir::new()?;
        let manager = SqliteManager::new(file(&dir, "b.db"));
        let conn = manager.connect().await?;
        assert!(!manager.is_broken(&conn));
        manager.close(conn.clone()).await;
        assert!(manager.is_broken(&conn));
        Ok(())
    }

    #[tokio::test]
    async fn a_memory_pool_refills_nothing_after_its_connection_is_discarded()
    -> Result<(), Box<dyn std::error::Error>> {
        let options = PoolOptions {
            min_size: 1,
            max_size: 1,
            idle_timeout: None,
            max_lifetime: None,
            ..PoolOptions::default()
        };
        let pool: CorePool<SqliteManager> = CorePool::new(
            SqliteManager::new(memory()),
            options,
            tokio::runtime::Handle::current(),
        )?;
        pool.open().await?;
        assert_eq!(pool.size(), 1);
        pool.acquire().await?.discard().await;
        assert_eq!(pool.size(), 0);
        // the next acquire's connect is the refused one, and it ends at once instead of waiting for the timeout
        let err = tokio::time::timeout(std::time::Duration::from_secs(10), acquire(&pool))
            .await?
            .map(|_| ());
        assert!(
            matches!(&err, Err(Error::Interface(text)) if text.contains("permanently broken")),
            "{err:?}"
        );
        Ok(())
    }
}
