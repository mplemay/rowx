use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

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
