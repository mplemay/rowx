use tokio::runtime::Handle;

use crate::conn::Connection;
use crate::dsn::ConnectOptions;
use crate::error::Error;

const MAINTENANCE_DATABASE: &str = "postgres";
// `createdb` connects here instead when the target is itself named `postgres`
const FALLBACK_MAINTENANCE_DATABASE: &str = "template1";
// NAMEDATALEN - 1: the server silently truncates longer names, so a lookup by the full name would miss
const FORCE_HELP: &str =
    "dropping with FORCE needs the pg_signal_backend role or ownership of the database";
const MAX_NAME_BYTES: usize = 63;

/// A database as `list_databases` reports it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DatabaseInfo {
    pub name: String,
    pub comment: Option<String>,
    // sessions connected right now (`pg_stat_database.numbackends`, visible to every role)
    pub sessions: i64,
}

/// The options to connect with when creating, dropping or looking for `target`.
///
/// The URL's own database is used unless it is the target. Otherwise it is `postgres`, or `template1`
/// when the target is itself named `postgres` (as `createdb` does). A URL without a database names the
/// user, as libpq does. Statements are never prepared on the connection.
#[must_use]
pub fn maintenance_options(opts: &ConnectOptions, target: &str) -> ConnectOptions {
    let own = opts.database.as_deref().or(opts.user.as_deref());
    let database = match own {
        Some(own) if own != target => own,
        _ if target == MAINTENANCE_DATABASE => FALLBACK_MAINTENANCE_DATABASE,
        _ => MAINTENANCE_DATABASE,
    };
    ConnectOptions {
        database: Some(database.to_owned()),
        statement_cache_size: 0,
        ..opts.clone()
    }
}

/// Quotes `name` as a Postgres identifier.
///
/// # Errors
/// `Interface` for an empty name, one with a NUL byte, which no identifier can hold, or one longer than
/// 63 bytes, which the server would truncate.
pub fn quote_ident(name: &str) -> Result<String, Error> {
    if name.is_empty() {
        return Err(Error::Interface(
            "a database name must not be empty".to_owned(),
        ));
    }
    if name.contains('\0') {
        return Err(Error::Interface(
            "a database name must not contain a NUL byte".to_owned(),
        ));
    }
    if name.len() > MAX_NAME_BYTES {
        return Err(Error::Interface(format!(
            "a database name must be at most {MAX_NAME_BYTES} bytes"
        )));
    }
    Ok(format!("\"{}\"", name.replace('"', "\"\"")))
}

#[must_use]
pub fn create_sql(quoted_name: &str, quoted_template: Option<&str>) -> String {
    quoted_template.map_or_else(
        || format!("CREATE DATABASE {quoted_name}"),
        |template| format!("CREATE DATABASE {quoted_name} TEMPLATE {template}"),
    )
}

#[must_use]
pub fn drop_sql(quoted_name: &str, force: bool) -> String {
    if force {
        format!("DROP DATABASE IF EXISTS {quoted_name} WITH (FORCE)")
    } else {
        format!("DROP DATABASE IF EXISTS {quoted_name}")
    }
}

/// Quotes `text` as a string literal, with `E''` when it holds a backslash so the result doesn't depend
/// on `standard_conforming_strings`.
///
/// # Errors
/// `Interface` for text with a NUL byte, which no literal can hold.
pub fn quote_literal(text: &str) -> Result<String, Error> {
    if text.contains('\0') {
        return Err(Error::Interface(
            "a database comment must not contain a NUL byte".to_owned(),
        ));
    }
    let escaped = text.replace('\'', "''");
    Ok(if text.contains('\\') {
        format!("E'{}'", escaped.replace('\\', "\\\\"))
    } else {
        format!("'{escaped}'")
    })
}

#[must_use]
pub fn comment_sql(quoted_name: &str, quoted_comment: &str) -> String {
    format!("COMMENT ON DATABASE {quoted_name} IS {quoted_comment}")
}

async fn connect_bounded(options: &ConnectOptions, handle: &Handle) -> Result<Connection, Error> {
    let connecting = Connection::connect(options, handle);
    match options.connect_timeout.filter(|t| !t.is_zero()) {
        Some(limit) => tokio::time::timeout(limit, connecting)
            .await
            .unwrap_or_else(|_| Err(Error::Interface("connection timed out".to_owned()))),
        None => connecting.await,
    }
}

// a managed provider may have no `postgres` database (3D000) or not let this role connect to it (42501)
fn can_fall_back(options: &ConnectOptions, err: &Error) -> bool {
    options.database.as_deref() == Some(MAINTENANCE_DATABASE)
        && matches!(err, Error::Db { sqlstate, .. } if &**sqlstate == "3D000" || &**sqlstate == "42501")
}

// connects to the maintenance database for `target`
async fn maintenance(
    options: &ConnectOptions,
    target: &str,
    handle: &Handle,
) -> Result<Connection, Error> {
    let primary = maintenance_options(options, target);
    match connect_bounded(&primary, handle).await {
        Err(err) if can_fall_back(&primary, &err) => {
            let fallback = ConnectOptions {
                database: Some(FALLBACK_MAINTENANCE_DATABASE.to_owned()),
                ..primary
            };
            // the original failure is the more useful one if template1 is refused too
            connect_bounded(&fallback, handle).await.map_err(|_| err)
        }
        other => other,
    }
}

async fn with_maintenance<T>(
    options: &ConnectOptions,
    target: &str,
    handle: &Handle,
    run: impl AsyncFnOnce(&Connection) -> Result<T, Error>,
) -> Result<T, Error> {
    let mut conn = maintenance(options, target, handle).await?;
    let result = run(&conn).await;
    conn.close().await;
    result
}

/// # Errors
/// `Interface` for a bad name, DSN, IO or TLS problem, `Db` for a server rejection.
pub async fn database_exists(
    options: &ConnectOptions,
    name: &str,
    handle: &Handle,
) -> Result<bool, Error> {
    quote_ident(name)?;
    with_maintenance(options, name, handle, async |conn| {
        let row = conn
            .client()?
            .query_opt("SELECT 1 FROM pg_database WHERE datname = $1", &[&name])
            .await?;
        Ok(row.is_some())
    })
    .await
}

/// Creates `name`, and sets `COMMENT ON DATABASE` when `comment` is given.
///
/// # Errors
/// As `database_exists`; `Db` (`42P04`) when the database exists already. A comment that can't be set drops
/// the new database again, so a failed call leaves nothing behind.
pub async fn create_database(
    options: &ConnectOptions,
    name: &str,
    template: Option<&str>,
    comment: Option<&str>,
    handle: &Handle,
) -> Result<(), Error> {
    let quoted = quote_ident(name)?;
    let create = create_sql(&quoted, template.map(quote_ident).transpose()?.as_deref());
    let comment = comment
        .map(|text| quote_literal(text).map(|literal| comment_sql(&quoted, &literal)))
        .transpose()?;
    with_maintenance(options, name, handle, async |conn| {
        conn.simple(&create).await?;
        if let Some(sql) = &comment
            && let Err(err) = conn.simple(sql).await
        {
            // best effort: the comment error is the one worth reporting
            let _ = conn.simple(&drop_sql(&quoted, true)).await;
            return Err(err);
        }
        Ok(())
    })
    .await
}

/// The databases whose name starts with `prefix`, by name.
///
/// # Errors
/// As `database_exists`.
pub async fn list_databases(
    options: &ConnectOptions,
    prefix: &str,
    handle: &Handle,
) -> Result<Vec<DatabaseInfo>, Error> {
    // no database is the target, so the URL's own database is used
    with_maintenance(options, "", handle, async |conn| {
        let rows = conn
            .client()?
            .query(
                "SELECT d.datname::text, shobj_description(d.oid, 'pg_database'), \
                 COALESCE((SELECT s.numbackends FROM pg_stat_database s WHERE s.datid = d.oid), 0)::int8 \
                 FROM pg_database d WHERE starts_with(d.datname::text, $1) ORDER BY d.datname",
                &[&prefix],
            )
            .await?;
        Ok(rows
            .iter()
            .map(|row| DatabaseInfo {
                name: row.get(0),
                comment: row.get(1),
                sessions: row.get(2),
            })
            .collect())
    })
    .await
}

/// Drops `name`; a missing database is not an error. `force` disconnects other sessions.
///
/// # Errors
/// As `database_exists`; `Db` when other sessions are connected and `force` is off.
pub async fn drop_database(
    options: &ConnectOptions,
    name: &str,
    force: bool,
    handle: &Handle,
) -> Result<(), Error> {
    let sql = drop_sql(&quote_ident(name)?, force);
    with_maintenance(options, name, handle, async |conn| conn.simple(&sql).await)
        .await
        .map_err(|err| if force { with_force_help(err) } else { err })
}

// `WITH (FORCE)` terminates other sessions, which needs `pg_signal_backend` or ownership
fn with_force_help(err: Error) -> Error {
    match err {
        Error::Db {
            sqlstate,
            message,
            detail,
            hint: None,
            position,
            routine,
        } if &*sqlstate == "42501" => Error::Db {
            hint: Some(FORCE_HELP.into()),
            sqlstate,
            message,
            detail,
            position,
            routine,
        },
        other => other,
    }
}

#[cfg(test)]
mod tests {
    use super::{
        comment_sql, create_sql, drop_sql, maintenance_options, quote_ident, quote_literal,
    };
    use crate::dsn::ConnectOptions;

    fn picked(database: Option<&str>, user: Option<&str>, target: &str) -> Option<String> {
        let opts = ConnectOptions {
            database: database.map(str::to_owned),
            user: user.map(str::to_owned),
            ..ConnectOptions::default()
        };
        let chosen = maintenance_options(&opts, target);
        assert_eq!(chosen.statement_cache_size, 0);
        chosen.database
    }

    #[test]
    fn uses_the_urls_own_database_unless_it_is_the_target() {
        assert_eq!(
            picked(Some("app"), Some("u"), "scratch").as_deref(),
            Some("app")
        );
        assert_eq!(
            picked(Some("template1"), Some("u"), "x").as_deref(),
            Some("template1")
        );
    }

    #[test]
    fn avoids_the_target_itself() {
        assert_eq!(
            picked(Some("app"), Some("u"), "app").as_deref(),
            Some("postgres")
        );
        assert_eq!(
            picked(Some("app"), Some("u"), "postgres").as_deref(),
            Some("app")
        );
    }

    #[test]
    fn a_target_named_postgres_uses_template1() {
        assert_eq!(
            picked(Some("postgres"), Some("u"), "postgres").as_deref(),
            Some("template1")
        );
        assert_eq!(picked(None, None, "postgres").as_deref(), Some("template1"));
    }

    #[test]
    fn no_database_targets_the_user_name() {
        assert_eq!(
            picked(None, Some("alice"), "scratch").as_deref(),
            Some("alice")
        );
        assert_eq!(
            picked(None, Some("alice"), "alice").as_deref(),
            Some("postgres")
        );
        assert_eq!(
            picked(None, Some("postgres"), "postgres").as_deref(),
            Some("template1")
        );
        assert_eq!(picked(None, None, "scratch").as_deref(), Some("postgres"));
    }

    #[test]
    fn quotes_literals() {
        assert_eq!(quote_literal("rowx:1@h:2").as_deref(), Ok("'rowx:1@h:2'"));
        assert_eq!(quote_literal("it's").as_deref(), Ok("'it''s'"));
        assert_eq!(quote_literal("a\\b'c").as_deref(), Ok("E'a\\\\b''c'"));
        assert!(quote_literal("a\0b").is_err());
        assert_eq!(
            comment_sql("\"a\"", "'c'"),
            "COMMENT ON DATABASE \"a\" IS 'c'"
        );
    }
    #[test]
    fn quotes_identifiers() {
        assert_eq!(quote_ident("app").as_deref(), Ok("\"app\""));
        assert_eq!(quote_ident("a-b").as_deref(), Ok("\"a-b\""));
        assert_eq!(quote_ident("a\"b").as_deref(), Ok("\"a\"\"b\""));
        assert_eq!(
            quote_ident("x\"; DROP DATABASE y; --").as_deref(),
            Ok("\"x\"\"; DROP DATABASE y; --\"")
        );
        assert!(quote_ident("").is_err());
        assert!(quote_ident("a\0b").is_err());
        assert!(quote_ident(&"a".repeat(63)).is_ok());
        assert!(quote_ident(&"a".repeat(64)).is_err());
        // the limit is in bytes, as the server counts it
        assert!(quote_ident(&"é".repeat(32)).is_err());
    }

    #[test]
    fn builds_statements() {
        assert_eq!(create_sql("\"a\"", None), "CREATE DATABASE \"a\"");
        assert_eq!(
            create_sql("\"a\"", Some("\"t\"")),
            "CREATE DATABASE \"a\" TEMPLATE \"t\""
        );
        assert_eq!(drop_sql("\"a\"", false), "DROP DATABASE IF EXISTS \"a\"");
        assert_eq!(
            drop_sql("\"a\"", true),
            "DROP DATABASE IF EXISTS \"a\" WITH (FORCE)"
        );
    }
}
