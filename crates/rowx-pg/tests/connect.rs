mod common;

use common::TestResult;
use rowx_pg::conn::Connection;
use rowx_pg::error::Error;
use tokio::runtime::Handle;

fn with_password(dsn: &str, password: &str) -> Result<String, Box<dyn std::error::Error>> {
    let config: tokio_postgres::Config = dsn.parse()?;
    let user = config.get_user().unwrap_or("rowx");
    let dbname = config.get_dbname().unwrap_or("rowx");
    let host = match config.get_hosts().first() {
        Some(tokio_postgres::config::Host::Tcp(h)) => h.clone(),
        _ => "127.0.0.1".to_owned(),
    };
    let port = config.get_ports().first().copied().unwrap_or(5432);
    Ok(format!(
        "host={host} port={port} user={user} password={password} dbname={dbname}"
    ))
}

#[tokio::test]
async fn connect_then_close() -> TestResult {
    let Some(dsn) = common::test_dsn()? else {
        return Ok(());
    };
    let mut conn = Connection::connect(&dsn, &Handle::current(), 256).await?;
    let row = conn.client()?.query_one("SELECT 1::int4", &[]).await?;
    assert_eq!(row.get::<_, i32>(0), 1);
    conn.close().await;
    conn.close().await;
    Ok(())
}

#[tokio::test]
async fn query_after_close_is_closed() -> TestResult {
    let Some(dsn) = common::test_dsn()? else {
        return Ok(());
    };
    let mut conn = Connection::connect(&dsn, &Handle::current(), 256).await?;
    conn.close().await;
    assert!(matches!(conn.client(), Err(Error::Closed)));
    Ok(())
}

#[tokio::test]
async fn bad_password_is_28p01_and_never_leaks() -> TestResult {
    let Some(dsn) = common::test_dsn()? else {
        return Ok(());
    };
    let secret = "wrong-secret-pw";
    let bad = with_password(&dsn, secret)?;
    let Err(err) = Connection::connect(&bad, &Handle::current(), 256).await else {
        return Err("connect unexpectedly succeeded".into());
    };
    assert!(
        matches!(&err, Error::Db { sqlstate, .. } if &**sqlstate == "28P01"),
        "{err:?}"
    );
    assert!(!err.to_string().contains(secret));
    assert!(!format!("{err:?}").contains(secret));
    Ok(())
}

#[tokio::test]
async fn refused_port_is_interface_error() -> TestResult {
    let secret = "refused-secret-pw";
    let dsn = format!("postgres://u:{secret}@127.0.0.1:1/db");
    let Err(err) = Connection::connect(&dsn, &Handle::current(), 256).await else {
        return Err("connect unexpectedly succeeded".into());
    };
    assert!(matches!(err, Error::Interface(_)), "{err:?}");
    assert!(!err.to_string().contains(secret));
    Ok(())
}

#[tokio::test]
async fn unsupported_sslmode_is_interface_error() -> TestResult {
    let Err(err) =
        Connection::connect("postgres://u@h/db?sslmode=require", &Handle::current(), 256).await
    else {
        return Err("connect unexpectedly succeeded".into());
    };
    assert!(matches!(err, Error::Interface(_)));
    Ok(())
}
