use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use tokio::runtime::Handle;
use tokio::task::JoinHandle;
use tokio_postgres::{Client, Statement};

use rowx_core::error::CoreError;

use crate::cancel::CancelTarget;
use crate::dsn::{ConnectOptions, ParsedDsn};
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
    pub async fn connect(options: &ConnectOptions, handle: &Handle) -> Result<Self, Error> {
        Self::connect_parsed(&options.build()?, options.statement_cache_size, handle).await
    }

    /// Connects from options that were parsed already, which a pool does once for all its connections.
    ///
    /// # Errors
    /// As `connect`, minus the DSN problems that parsing already reported.
    pub async fn connect_parsed(
        parsed: &ParsedDsn,
        statement_cache_size: usize,
        handle: &Handle,
    ) -> Result<Self, Error> {
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
        let options = ConnectOptions {
            statement_cache_size: 0,
            ..ConnectOptions::from_dsn(dsn)?
        };
        let mut conn = Self::connect(&options, handle).await?;
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
        let client = self.client.as_ref().ok_or(CoreError::Closed)?;
        if self.broken.load(Ordering::Acquire) {
            return Err(CoreError::Broken.into());
        }
        Ok(client)
    }

    /// The client together with the statement cache, for operations that prepare through it.
    ///
    /// # Errors
    /// `Closed` after `close()`, `Broken` once the connection task has failed.
    pub fn parts(&mut self) -> Result<(&Client, &mut StmtCache<Statement>), Error> {
        let client = self.client.as_ref().ok_or(CoreError::Closed)?;
        if self.broken.load(Ordering::Acquire) {
            return Err(CoreError::Broken.into());
        }
        Ok((client, &mut self.cache))
    }

    /// Runs `sql` with the simple query protocol, so nothing is prepared or cached.
    ///
    /// # Errors
    /// `Closed`/`Broken` for a dead connection, `Db` when the server rejects the statement.
    pub async fn simple(&self, sql: &str) -> Result<(), Error> {
        self.client()?.batch_execute(sql).await?;
        Ok(())
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

const ISOLATION_LEVELS: [&str; 4] = [
    "read uncommitted",
    "read committed",
    "repeatable read",
    "serializable",
];

/// The `BEGIN` for the transaction helper.
///
/// # Errors
/// `Interface` for an isolation level Postgres does not have.
pub fn begin_sql(
    isolation: Option<&str>,
    readonly: bool,
    deferrable: bool,
) -> Result<String, Error> {
    let mut sql = String::from("BEGIN");
    if let Some(level) = isolation {
        let level = level.split_whitespace().collect::<Vec<_>>().join(" ");
        let level = level.to_ascii_lowercase();
        if !ISOLATION_LEVELS.contains(&level.as_str()) {
            return Err(Error::Interface(format!(
                "unknown isolation level {level:?}; use one of {ISOLATION_LEVELS:?}"
            )));
        }
        sql.push_str(" ISOLATION LEVEL ");
        sql.push_str(&level.to_ascii_uppercase());
    }
    if readonly {
        sql.push_str(" READ ONLY");
    }
    if deferrable {
        sql.push_str(" DEFERRABLE");
    }
    Ok(sql)
}

#[must_use]
pub fn savepoint_sql(n: u32) -> String {
    format!("SAVEPOINT rowx_sp_{n}")
}

#[must_use]
pub fn release_savepoint_sql(n: u32) -> String {
    format!("RELEASE SAVEPOINT rowx_sp_{n}")
}

#[must_use]
pub fn rollback_to_savepoint_sql(n: u32) -> String {
    format!("ROLLBACK TO SAVEPOINT rowx_sp_{n}")
}

/// Whether `sql` opens a transaction or savepoint by hand, judged from its first keyword.
#[must_use]
pub fn opens_transaction(sql: &str) -> bool {
    let mut rest = sql.trim_start();
    // leading comments do not change the statement
    loop {
        if let Some(after) = rest.strip_prefix("--") {
            rest = after
                .split_once('\n')
                .map_or("", |(_, tail)| tail)
                .trim_start();
        } else if let Some(after) = rest.strip_prefix("/*") {
            rest = skip_block_comment(after).trim_start();
        } else {
            break;
        }
    }
    let word: String = rest
        .chars()
        .take_while(char::is_ascii_alphabetic)
        .collect::<String>()
        .to_ascii_uppercase();
    matches!(word.as_str(), "BEGIN" | "START" | "SAVEPOINT")
}

// block comments nest in Postgres; `after` starts just past the opening `/*`
fn skip_block_comment(after: &str) -> &str {
    let mut depth = 1_usize;
    let mut rest = after;
    while depth > 0 {
        match (rest.find("/*"), rest.find("*/")) {
            (Some(open), Some(close)) if open < close => {
                depth += 1;
                rest = &rest[open + 2..];
            }
            (_, Some(close)) => {
                depth -= 1;
                rest = &rest[close + 2..];
            }
            _ => return "",
        }
    }
    rest
}

#[cfg(test)]
mod tests {
    use super::{begin_sql, opens_transaction, release_savepoint_sql, savepoint_sql};

    #[test]
    fn builds_begin_statements() {
        assert_eq!(begin_sql(None, false, false).as_deref(), Ok("BEGIN"));
        assert_eq!(
            begin_sql(Some("Repeatable  Read"), true, false).as_deref(),
            Ok("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        );
        assert_eq!(
            begin_sql(Some("serializable"), true, true).as_deref(),
            Ok("BEGIN ISOLATION LEVEL SERIALIZABLE READ ONLY DEFERRABLE")
        );
        assert!(begin_sql(Some("x; DROP TABLE t"), false, false).is_err());
    }

    #[test]
    fn builds_savepoint_statements() {
        assert_eq!(savepoint_sql(2), "SAVEPOINT rowx_sp_2");
        assert_eq!(release_savepoint_sql(2), "RELEASE SAVEPOINT rowx_sp_2");
    }

    #[test]
    fn detects_the_transaction_keywords() {
        for sql in [
            "BEGIN",
            "begin isolation level serializable",
            "  Start TRANSACTION",
            "-- note\nSAVEPOINT a",
            "/* x */ begin;",
            "/* outer /* inner */ outer */ BEGIN",
        ] {
            assert!(opens_transaction(sql), "{sql}");
        }
    }

    #[test]
    fn ignores_everything_else() {
        for sql in [
            "SELECT 1",
            "COMMIT",
            "",
            "-- BEGIN\nSELECT 1",
            "BEGINNING()",
            "startx",
        ] {
            assert!(!opens_transaction(sql), "{sql}");
        }
    }
}
