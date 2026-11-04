use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

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
pub const RESET_QUERY: &str = "RESET SESSION AUTHORIZATION; RESET ALL; SELECT pg_catalog.pg_advisory_unlock_all(); CLOSE ALL; UNLISTEN *; DISCARD TEMP;";

/// Fails unless the session is idle.
///
/// Inside a transaction `now()` is the transaction's start, so it differs from `statement_timestamp()`. Names are
/// qualified because the batch runs under the previous checkout's `search_path`.
pub(crate) const IDLE_GUARD: &str = "SELECT 1 OPERATOR(pg_catalog./) (pg_catalog.now() OPERATOR(pg_catalog.=) pg_catalog.statement_timestamp())::pg_catalog.int4;";

/// The simple queries a release sends in order, empty when there is nothing to send.
///
/// `rollback` is the "maybe in a transaction" flag: a set flag sends `ROLLBACK` first (even without a reset query),
/// a clear one puts the idle guard in front of the reset query. The default query goes out as one batch; a custom one
/// is sent alone, because a multi-statement batch is an implicit transaction and statements such as `DISCARD ALL` or
/// `VACUUM` cannot run in one.
#[must_use]
pub fn reset_batches(query: Option<&str>, rollback: bool) -> Vec<String> {
    let first = if rollback { "ROLLBACK;" } else { IDLE_GUARD };
    match query {
        None if rollback => vec!["ROLLBACK".to_owned()],
        None => Vec::new(),
        Some(query) if query == RESET_QUERY => vec![format!("{first} {query}")],
        Some(query) => vec![first.trim_end_matches(';').to_owned(), query.to_owned()],
    }
}

pub type Pool = rowx_core::pool::Pool<PgManager>;
pub type Pooled = rowx_core::pool::Pooled<PgManager>;
pub use rowx_core::pool::PoolOptions;

/// A pooled connection: what a `Connection` is built from, plus its cancel plumbing.
#[derive(Debug, Clone)]
pub struct PgConn {
    pub cancel: CoreCancel<PgBackend>,
    /// Set when the caller already ran the reset query, so the release does not send it twice.
    pub reset_done: Arc<AtomicBool>,
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
            reset_done: Arc::new(AtomicBool::new(false)),
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
        // the caller's own reset covers a clean session, but not one that is in a transaction again
        if conn.reset_done.swap(false, Ordering::AcqRel) && !rollback {
            return Ok(());
        }
        let batches = reset_batches(self.reset_query.as_deref(), rollback);
        let guard = conn.cancel.backend.conn.lock().await;
        let mut result = Ok(());
        for sql in &batches {
            result = guard.simple(sql).await;
            if result.is_err() {
                break;
            }
        }
        drop(guard);
        result
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
    fn manager_debug_output_never_carries_a_secret() -> Result<(), Box<dyn std::error::Error>> {
        let runtime = tokio::runtime::Builder::new_current_thread().build()?;
        let options = ConnectOptions::from_dsn("host=h password=Zq9pwSecret")?;
        let manager = PgManager::new(&options, runtime.handle().clone(), None)?;
        assert!(!format!("{manager:?}").contains("Zq9"));
        Ok(())
    }

    #[test]
    fn the_reset_batch_is_guarded_only_when_the_flag_is_clear() {
        assert_eq!(reset_batches(None, false).len(), 0);
        assert_eq!(reset_batches(None, true), ["ROLLBACK"]);
        assert_eq!(
            reset_batches(Some(RESET_QUERY), true),
            [format!("ROLLBACK; {RESET_QUERY}")]
        );
        assert_eq!(
            reset_batches(Some(RESET_QUERY), false),
            [format!("{IDLE_GUARD} {RESET_QUERY}")]
        );
        // a custom query is sent alone so it can be DISCARD ALL or VACUUM
        assert_eq!(
            reset_batches(Some("DISCARD ALL"), true),
            ["ROLLBACK", "DISCARD ALL"]
        );
        assert_eq!(
            reset_batches(Some("DISCARD ALL"), false),
            [IDLE_GUARD.trim_end_matches(';'), "DISCARD ALL"]
        );
        assert!(!RESET_QUERY.contains("DEALLOCATE"));
    }

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
