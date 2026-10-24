use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

use tokio::runtime::Handle;
use tokio::task::JoinHandle;
use tokio_postgres::{Client, NoTls};

use crate::dsn;
use crate::error::Error;

#[derive(Debug)]
pub struct Connection {
    client: Option<Client>,
    task: Option<JoinHandle<()>>,
    broken: Arc<AtomicBool>,
}

impl Connection {
    /// # Errors
    /// `Interface` for DSN, IO or unsupported-TLS problems, `Db` for a server rejection.
    pub async fn connect(dsn: &str, handle: &Handle) -> Result<Self, Error> {
        let parsed = dsn::parse(dsn)?;
        let (client, connection) = parsed.config.connect(NoTls).await?;
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
        })
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

    // idempotent: the client drops first so the server sees a clean terminate, then the task is awaited
    pub async fn close(&mut self) {
        self.client = None;
        if let Some(task) = self.task.take() {
            // a join error means the task panicked or was cancelled; the connection is gone either way
            let _ = task.await;
        }
    }
}
