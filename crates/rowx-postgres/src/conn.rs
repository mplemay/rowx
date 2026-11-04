use std::future::poll_fn;
use std::sync::atomic::{AtomicBool, AtomicU8, Ordering};
use std::sync::{Arc, OnceLock};
use std::time::Duration;

use tokio::runtime::Handle;
use tokio::task::JoinHandle;
use tokio_postgres::{Client, Statement};

use rowx_core::error::CoreError;

use crate::cancel::CancelTarget;
use crate::dsn::{ConnectOptions, ParsedDsn};
use crate::error::{Drift, Error};
use crate::stmt_cache::StmtCache;

const CLEAN: u8 = 0;
const ENCODING: u8 = 1;
const STRINGS: u8 = 2;

// the server reports `SET client_encoding = 'UNICODE'` under the alias it was given
fn is_utf8(name: &str) -> bool {
    name.eq_ignore_ascii_case("UTF8") || name.eq_ignore_ascii_case("UNICODE")
}

/// Records that the session left the settings rowx encodes for. The taint is sticky: the first reason
/// stays for the life of the connection, whatever the session sets afterwards.
#[derive(Debug, Default)]
pub struct Taint {
    reason: AtomicU8,
    // written before `reason` is published, so a reader that sees ENCODING always finds the name
    encoding: OnceLock<Box<str>>,
}

impl Taint {
    /// Looks at the runtime parameters the server reported so far; `parameter` reads one by name.
    pub fn observe<'a>(&self, parameter: impl Fn(&str) -> Option<&'a str>) {
        if let Some(name) = parameter("client_encoding").filter(|name| !is_utf8(name)) {
            // a server-supplied name goes into an error message, so it is bounded and made printable
            let shown: String = name
                .chars()
                .map(|c| if c.is_ascii_graphic() { c } else { '?' })
                .take(64)
                .collect();
            let _ = self.encoding.set(shown.into());
            self.mark(ENCODING);
        }
        if parameter("standard_conforming_strings") == Some("off") {
            self.mark(STRINGS);
        }
    }

    fn mark(&self, reason: u8) {
        // a failed exchange means an earlier reason already holds
        let _ = self
            .reason
            .compare_exchange(CLEAN, reason, Ordering::AcqRel, Ordering::Acquire);
    }

    #[must_use]
    pub fn drift(&self) -> Option<Drift> {
        match self.reason.load(Ordering::Acquire) {
            ENCODING => Some(Drift::ClientEncoding(
                self.encoding.get().cloned().unwrap_or_default(),
            )),
            STRINGS => Some(Drift::StandardConformingStrings),
            _ => None,
        }
    }
}

#[derive(Debug)]
pub struct Connection {
    client: Option<Client>,
    task: Option<JoinHandle<()>>,
    broken: Arc<AtomicBool>,
    taint: Arc<Taint>,
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
        let connector = parsed.tls.connector().map_err(|e| parsed.redact_error(e))?;
        let (client, mut connection) = parsed
            .config
            .connect(connector.clone())
            .await
            .map_err(|e| parsed.redact_error(Error::from(e)))?;
        let cancel = CancelTarget::new(client.cancel_token(), &parsed.config, connector)
            .map_err(|e| parsed.redact_error(e))?;
        let broken = Arc::new(AtomicBool::new(false));
        let flag = Arc::clone(&broken);
        // startup parameters are in already (a role-level `standard_conforming_strings = off`), so
        // the first call fails without waiting for the task's first poll
        let taint = Arc::new(Taint::default());
        taint.observe(|name| connection.parameter(name));
        let watch = Arc::clone(&taint);
        let task = handle.spawn(async move {
            // ParameterStatus is consumed inside poll_read and never returned by poll_message, so
            // the check runs on every poll, including Pending
            loop {
                let message = poll_fn(|cx| {
                    let polled = connection.poll_message(cx);
                    watch.observe(|name| connection.parameter(name));
                    polled
                })
                .await;
                match message {
                    // notices and notifications were only logged before, which no one reads
                    Some(Ok(_)) => {}
                    Some(Err(_)) => {
                        flag.store(true, Ordering::Release);
                        return;
                    }
                    None => return,
                }
            }
        });
        Ok(Self {
            client: Some(client),
            task: Some(task),
            broken,
            taint,
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
    /// `Closed` after `close()`, `Broken` once the connection task has failed, `Drift` once the session
    /// changed `client_encoding` or `standard_conforming_strings`.
    pub fn client(&self) -> Result<&Client, Error> {
        let client = self.client.as_ref().ok_or(CoreError::Closed)?;
        if self.broken.load(Ordering::Acquire) {
            return Err(CoreError::Broken.into());
        }
        if let Some(drift) = self.taint.drift() {
            return Err(Error::Drift(drift));
        }
        Ok(client)
    }

    /// Passes `result` through, unless the session drifted while the operation ran. The rows of the
    /// statement that changed the encoding were already sent in it, so they may be mis-decoded.
    ///
    /// # Errors
    /// `Drift` when the taint is set; otherwise `result`'s own error.
    pub fn settled<T>(&self, result: Result<T, Error>) -> Result<T, Error> {
        let value = result?;
        self.taint
            .drift()
            .map_or(Ok(value), |d| Err(Error::Drift(d)))
    }

    /// The client together with the statement cache, for operations that prepare through it.
    ///
    /// # Errors
    /// `Closed` after `close()`, `Broken` once the connection task has failed, `Drift` once the session
    /// changed `client_encoding` or `standard_conforming_strings`.
    pub fn parts(&mut self) -> Result<(&Client, &mut StmtCache<Statement>), Error> {
        let client = self.client.as_ref().ok_or(CoreError::Closed)?;
        if self.broken.load(Ordering::Acquire) {
            return Err(CoreError::Broken.into());
        }
        if let Some(drift) = self.taint.drift() {
            return Err(Error::Drift(drift));
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
    // leading comments and empty statements (`;`) do not change the statement
    loop {
        if let Some(after) = rest.strip_prefix(';') {
            rest = after.trim_start();
        } else if let Some(after) = rest.strip_prefix("--") {
            // the server ends a line comment at `\r` as well as `\n`
            rest = after
                .split_once(['\n', '\r'])
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
    use super::{Taint, begin_sql, opens_transaction, release_savepoint_sql, savepoint_sql};
    use crate::error::Drift;

    fn observed(taint: &Taint, encoding: &str, strings: &str) {
        taint.observe(|name| match name {
            "client_encoding" => Some(encoding),
            "standard_conforming_strings" => Some(strings),
            _ => None,
        });
    }

    #[test]
    fn utf8_with_conforming_strings_never_taints() {
        let taint = Taint::default();
        observed(&taint, "UTF8", "on");
        taint.observe(|_| None);
        assert_eq!(taint.drift(), None);
    }

    #[test]
    fn the_unicode_alias_is_utf8() {
        for name in ["UNICODE", "unicode", "utf8", "Utf8"] {
            let taint = Taint::default();
            observed(&taint, name, "on");
            assert_eq!(taint.drift(), None, "{name}");
        }
    }

    #[test]
    fn another_encoding_taints_and_names_it() {
        let taint = Taint::default();
        observed(&taint, "SJIS", "on");
        assert_eq!(taint.drift(), Some(Drift::ClientEncoding("SJIS".into())));
        assert_eq!(
            taint.drift().map(|d| d.to_string()).as_deref(),
            Some("the session changed client_encoding to SJIS; rowx requires UTF8")
        );
    }

    #[test]
    fn standard_conforming_strings_off_taints() {
        let taint = Taint::default();
        observed(&taint, "UTF8", "off");
        assert_eq!(taint.drift(), Some(Drift::StandardConformingStrings));
    }

    #[test]
    fn the_taint_is_sticky_and_keeps_the_first_reason() {
        let taint = Taint::default();
        observed(&taint, "UTF8", "off");
        observed(&taint, "SJIS", "on");
        observed(&taint, "UTF8", "on");
        assert_eq!(taint.drift(), Some(Drift::StandardConformingStrings));
        let other = Taint::default();
        observed(&other, "LATIN1", "on");
        observed(&other, "UTF8", "on");
        assert_eq!(other.drift(), Some(Drift::ClientEncoding("LATIN1".into())));
    }

    #[test]
    fn a_hostile_encoding_name_is_bounded_and_printable() {
        let taint = Taint::default();
        let name = format!("a\nb{}", "x".repeat(200));
        observed(&taint, &name, "on");
        let text = taint.drift().map(|d| d.to_string()).unwrap_or_default();
        assert!(!text.contains('\n'));
        assert!(text.len() < 150);
    }

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
            "-- note\rBEGIN",
            "-- a\r\n-- b\rSTART TRANSACTION",
            "/* x */ begin;",
            "/* outer /* inner */ outer */ BEGIN",
            ";BEGIN",
            ";;START TRANSACTION",
            "/**/;BEGIN",
            "; -- x\nBEGIN",
            " ; /* a */ ; SAVEPOINT a",
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
            "-- BEGIN\rSELECT 1",
            "BEGINNING()",
            "startx",
            ";",
            "; SELECT 1",
            ";-- BEGIN\nSELECT 1",
        ] {
            assert!(!opens_transaction(sql), "{sql}");
        }
    }
}
