use tokio::runtime::Handle;

use crate::conn::Connection;
use crate::dsn::ConnectOptions;
use crate::error::Error;

const MAINTENANCE_DATABASE: &str = "postgres";
// `createdb` connects here instead when the target is itself named `postgres`
const FALLBACK_MAINTENANCE_DATABASE: &str = "template1";

/// The database to connect to when creating, dropping or looking for `target`.
#[must_use]
pub fn maintenance_database(target: &str) -> &'static str {
    if target == MAINTENANCE_DATABASE {
        FALLBACK_MAINTENANCE_DATABASE
    } else {
        MAINTENANCE_DATABASE
    }
}

/// Quotes `name` as a Postgres identifier.
///
/// # Errors
/// `Interface` for an empty name or one with a NUL byte, which no identifier can hold.
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

// connects with `options` but to the maintenance database for `target`; no statement is ever prepared
async fn maintenance(
    options: &ConnectOptions,
    target: &str,
    handle: &Handle,
) -> Result<Connection, Error> {
    let options = ConnectOptions {
        database: Some(maintenance_database(target).to_owned()),
        statement_cache_size: 0,
        ..options.clone()
    };
    let connecting = Connection::connect(&options, handle);
    match options.connect_timeout.filter(|t| !t.is_zero()) {
        Some(limit) => tokio::time::timeout(limit, connecting)
            .await
            .unwrap_or_else(|_| Err(Error::Interface("connection timed out".to_owned()))),
        None => connecting.await,
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

/// # Errors
/// As `database_exists`; `Db` (`42P04`) when the database exists already.
pub async fn create_database(
    options: &ConnectOptions,
    name: &str,
    template: Option<&str>,
    handle: &Handle,
) -> Result<(), Error> {
    let sql = create_sql(
        &quote_ident(name)?,
        template.map(quote_ident).transpose()?.as_deref(),
    );
    with_maintenance(options, name, handle, async |conn| conn.simple(&sql).await).await
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
    with_maintenance(options, name, handle, async |conn| conn.simple(&sql).await).await
}

#[cfg(test)]
mod tests {
    use super::{create_sql, drop_sql, maintenance_database, quote_ident};

    #[test]
    fn picks_the_maintenance_database() {
        assert_eq!(maintenance_database("app"), "postgres");
        assert_eq!(maintenance_database("template1"), "postgres");
        assert_eq!(maintenance_database("postgres"), "template1");
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
