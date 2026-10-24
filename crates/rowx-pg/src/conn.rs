use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use tokio::runtime::Handle;
use tokio::task::JoinHandle;
use tokio_postgres::{Client, Statement};

use crate::cancel::CancelTarget;
use crate::dsn;
use crate::error::Error;
use crate::stmt_cache::StmtCache;

#[derive(Debug)]
pub struct Connection {
    client: Option<Client>,
    task: Option<JoinHandle<()>>,
    broken: Arc<AtomicBool>,
    cache: StmtCache<Statement>,
    cancel: CancelTarget,
}

impl Connection {
    /// # Errors
    /// `Interface` for DSN, IO or TLS problems, `Db` for a server rejection.
    pub async fn connect(
        dsn: &str,
        handle: &Handle,
        statement_cache_size: usize,
    ) -> Result<Self, Error> {
        let parsed = dsn::parse(dsn)?;
        let connector = parsed.tls.connector()?;
        let (client, connection) = parsed.config.connect(connector.clone()).await?;
        let cancel = CancelTarget::new(client.cancel_token(), &parsed.config, connector)?;
        let broken = Arc::new(AtomicBool::new(false));
        let flag = Arc::clone(&broken);
        let task = handle.spawn(async move {
            if connection.await.is_err() {
                flag.store(true, Ordering::Release);
            }
        });
        Ok(Self {
            client: Some(client),
            task: Some(task),
            broken,
            cache: StmtCache::new(statement_cache_size),
            cancel,
        })
    }

    /// Connects for `rowx check`: a read-only session with bounded timeouts, and no statement cache.
    ///
    /// # Errors
    /// As `connect`, plus `Db` when the server rejects a session setting.
    pub async fn connect_checker(
        dsn: &str,
        handle: &Handle,
        statement_timeout: Duration,
    ) -> Result<Self, Error> {
        let mut conn = Self::connect(dsn, handle, 0).await?;
        let settings = format!(
            "SET default_transaction_read_only = on; \
             SET statement_timeout = '{}ms'; \
             SET lock_timeout = '2s'; \
             SET idle_in_transaction_session_timeout = '30s'; \
             SET application_name = 'rowx-check'",
            statement_timeout.as_millis()
        );
        let applied = match conn.client() {
            Ok(client) => client.batch_execute(&settings).await.map_err(Error::from),
            Err(e) => Err(e),
        };
        if let Err(e) = applied {
            conn.close().await;
            return Err(e);
        }
        Ok(conn)
    }

    #[must_use]
    pub fn cancel_target(&self) -> CancelTarget {
        self.cancel.clone()
    }

    /// # Errors
    /// `Closed` after `close()`, `Broken` once the connection task has failed.
    pub fn client(&self) -> Result<&Client, Error> {
        let client = self.client.as_ref().ok_or(Error::Closed)?;
        if self.broken.load(Ordering::Acquire) {
            return Err(Error::Broken);
        }
        Ok(client)
    }

    /// The client together with the statement cache, for operations that prepare through it.
    ///
    /// # Errors
    /// `Closed` after `close()`, `Broken` once the connection task has failed.
    pub fn parts(&mut self) -> Result<(&Client, &mut StmtCache<Statement>), Error> {
        let client = self.client.as_ref().ok_or(Error::Closed)?;
        if self.broken.load(Ordering::Acquire) {
            return Err(Error::Broken);
        }
        Ok((client, &mut self.cache))
    }

    // idempotent: the client drops first so the server sees a clean terminate, then the task is awaited
    pub async fn close(&mut self) {
        self.cache.clear();
        self.client = None;
        if let Some(task) = self.task.take() {
            // a join error means the task panicked or was cancelled; the connection is gone either way
            let _ = task.await;
        }
    }
}
