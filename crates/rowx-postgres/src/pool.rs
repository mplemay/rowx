use std::sync::Arc;

use rowx_core::cancel::Cancel as CoreCancel;
use rowx_core::pool::{Manage, PoolError};
use rowx_core::state::{Phase, State};
use tokio::runtime::Handle;
use tokio::sync::Mutex;

use crate::cancel::PgBackend;
use crate::conn::Connection;
use crate::dsn::{ConnectOptions, ParsedDsn};
use crate::error::Error;

/// The reset a pool runs on release; the same text as `rowx.postgres.RESET_QUERY`.
pub const RESET_QUERY: &str = "SELECT pg_advisory_unlock_all(); CLOSE ALL; UNLISTEN *; RESET ALL;";

pub type Pool = rowx_core::pool::Pool<PgManager>;
pub type Pooled = rowx_core::pool::Pooled<PgManager>;
pub use rowx_core::pool::PoolOptions;

/// A pooled connection: what a `Connection` is built from, plus its cancel plumbing.
#[derive(Debug, Clone)]
pub struct PgConn {
    pub cancel: CoreCancel<PgBackend>,
}

impl PgConn {
    #[must_use]
    pub const fn state(&self) -> &Arc<State> {
        &self.cancel.state
    }
}

/// Makes and maintains the Postgres connections of a pool. The options are parsed once.
#[derive(Debug)]
pub struct PgManager {
    parsed: ParsedDsn,
    statement_cache_size: usize,
    handle: Handle,
    reset_query: Option<String>,
}

impl PgManager {
    /// # Errors
    /// `Interface` for a DSN setting the driver rejects.
    pub fn new(
        options: &ConnectOptions,
        handle: Handle,
        reset_query: Option<String>,
    ) -> Result<Self, Error> {
        Ok(Self {
            parsed: options.build()?,
            statement_cache_size: options.statement_cache_size,
            handle,
            reset_query,
        })
    }
}

/// Whether `err` says a cached plan went stale, so the pool should bump its schema epoch.
#[must_use]
pub fn is_stale_plan(err: &Error) -> bool {
    matches!(err, Error::Db { sqlstate, .. } if matches!(&**sqlstate, "0A000" | "26000"))
}

impl From<PoolError<Self>> for Error {
    fn from(err: PoolError<Self>) -> Self {
        match err {
            PoolError::Core(core) => Self::Core(core),
            PoolError::Manage(err) => err,
        }
    }
}

impl Manage for PgManager {
    type Conn = PgConn;
    type Error = Error;
    type Backend = PgBackend;

    async fn connect(&self) -> Result<PgConn, Error> {
        let conn =
            Connection::connect_parsed(&self.parsed, self.statement_cache_size, &self.handle)
                .await?;
        Ok(PgConn {
            cancel: CoreCancel {
                state: Arc::new(State::default()),
                backend: PgBackend {
                    target: conn.cancel_target(),
                    conn: Arc::new(Mutex::new(conn)),
                },
                handle: self.handle.clone(),
            },
        })
    }

    fn state<'a>(&self, conn: &'a PgConn) -> &'a Arc<State> {
        conn.state()
    }

    fn backend(&self, conn: &PgConn) -> PgBackend {
        conn.cancel.backend.clone()
    }

    async fn ping(&self, conn: &PgConn) -> Result<(), Error> {
        conn.cancel.backend.conn.lock().await.simple("").await
    }

    async fn reset(&self, conn: &PgConn, rollback: bool) -> Result<(), Error> {
        // an unconditional ROLLBACK would log a server WARNING on every idle release
        let sql = match (rollback, self.reset_query.as_deref()) {
            (false, None) => return Ok(()),
            (true, None) => "ROLLBACK".to_owned(),
            (false, Some(query)) => query.to_owned(),
            (true, Some(query)) => format!("ROLLBACK; {query}"),
        };
        conn.cancel.backend.conn.lock().await.simple(&sql).await
    }

    fn is_broken(&self, conn: &PgConn) -> bool {
        if matches!(conn.state().phase(), Phase::Broken | Phase::Closed) {
            return true;
        }
        // a locked connection is in use, so it is not dead
        conn.cancel
            .backend
            .conn
            .try_lock()
            .is_ok_and(|c| c.client().is_err())
    }

    async fn settled(&self, conn: &PgConn) -> bool {
        conn.state().wait_settled().await.is_ok()
    }

    async fn forget_statements(&self, conn: &PgConn) {
        if let Ok((_, cache)) = conn.cancel.backend.conn.lock().await.parts() {
            cache.clear();
        }
    }

    async fn close(&self, conn: PgConn) {
        // a locked connection has an operation running (even when `terminate` already closed its
        // state), which would hold the lock for its full length
        let running = conn.cancel.backend.conn.try_lock().is_err();
        conn.state().close();
        if running {
            // a failed request is fine because dropping the client ends the query anyway
            let _ = conn.cancel.backend.target.request().await;
        }
        conn.cancel.backend.conn.lock().await.close().await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stale_plan_codes() {
        let db = |code: &str| Error::Db {
            sqlstate: code.into(),
            message: "".into(),
            detail: None,
            hint: None,
            position: None,
            routine: None,
        };
        assert!(is_stale_plan(&db("0A000")));
        assert!(is_stale_plan(&db("26000")));
        assert!(!is_stale_plan(&db("42P01")));
        assert!(!is_stale_plan(&Error::Interface("x".into())));
    }
}
