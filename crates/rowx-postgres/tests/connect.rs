// pub, so the shared helpers are reachable from the crate root and need no visibility waiver
pub mod common;

use common::TestResult;
use common::fake_server::{Auth, FakeServer, Reply, Script, Tls};
use rowx_core::error::CoreError;
use rowx_postgres::conn::Connection;
use rowx_postgres::dsn::ConnectOptions;
use rowx_postgres::error::{Drift, Error};
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
    let mut conn =
        Connection::connect(&ConnectOptions::from_dsn(&dsn)?, &Handle::current()).await?;
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
    let mut conn =
        Connection::connect(&ConnectOptions::from_dsn(&dsn)?, &Handle::current()).await?;
    conn.close().await;
    assert!(matches!(conn.client(), Err(Error::Core(CoreError::Closed))));
    Ok(())
}

#[tokio::test]
async fn bad_password_is_28p01_and_never_leaks() -> TestResult {
    let Some(dsn) = common::test_dsn()? else {
        return Ok(());
    };
    let secret = "wrong-secret-pw";
    let bad = with_password(&dsn, secret)?;
    let Err(err) = Connection::connect(&ConnectOptions::from_dsn(&bad)?, &Handle::current()).await
    else {
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
    let Err(err) = Connection::connect(&ConnectOptions::from_dsn(&dsn)?, &Handle::current()).await
    else {
        return Err("connect unexpectedly succeeded".into());
    };
    assert!(matches!(err, Error::Interface(_)), "{err:?}");
    assert!(!err.to_string().contains(secret));
    Ok(())
}

#[tokio::test]
async fn client_certificate_settings_are_interface_errors() -> TestResult {
    let Err(err) = ConnectOptions::from_dsn("postgres://u@h/db?sslcert=/x") else {
        return Err("parse unexpectedly succeeded".into());
    };
    assert!(matches!(err, Error::Interface(_)));
    Ok(())
}

async fn first_failure(conn: &Connection) -> Result<Error, Box<dyn std::error::Error>> {
    // the ParameterStatus is processed right after the SET completes, so retry instead of sleeping
    for _ in 0..10_000 {
        if let Err(err) = conn.client() {
            return Ok(err);
        }
        tokio::task::yield_now().await;
    }
    Err("the connection never failed".into())
}

#[tokio::test]
async fn a_changed_client_encoding_fails_client_and_parts() -> TestResult {
    let Some(dsn) = common::test_dsn()? else {
        return Ok(());
    };
    let mut conn =
        Connection::connect(&ConnectOptions::from_dsn(&dsn)?, &Handle::current()).await?;
    conn.simple("SET client_encoding = 'UTF8'").await?;
    conn.client()?;
    assert_eq!(conn.settled(Ok(1)), Ok(1));
    conn.simple("SET client_encoding = 'SJIS'").await?;
    let err = first_failure(&conn).await?;
    assert_eq!(err, Error::Drift(Drift::ClientEncoding("SJIS".into())));
    // an operation that finished after the drift reports it instead of its result
    assert_eq!(conn.settled(Ok(1)), Err(err));
    assert!(matches!(conn.parts(), Err(Error::Drift(_))));
    // sticky: going back to UTF8 does not clear it
    assert!(conn.simple("SET client_encoding = 'UTF8'").await.is_err());
    assert!(matches!(conn.client(), Err(Error::Drift(_))));
    conn.close().await;
    Ok(())
}

#[tokio::test]
async fn standard_conforming_strings_off_fails_client() -> TestResult {
    let Some(dsn) = common::test_dsn()? else {
        return Ok(());
    };
    let conn = Connection::connect(&ConnectOptions::from_dsn(&dsn)?, &Handle::current()).await?;
    conn.simple("SET standard_conforming_strings = off").await?;
    let err = first_failure(&conn).await?;
    assert_eq!(err, Error::Drift(Drift::StandardConformingStrings));
    Ok(())
}

fn fake_options(port: u16, extra: &str) -> Result<ConnectOptions, Error> {
    ConnectOptions::from_dsn(&format!(
        "host=127.0.0.1 port={port} user=u password=fake-secret-pw dbname=d {extra}"
    ))
}

async fn connect_fake(port: u16, extra: &str) -> Result<Connection, Error> {
    Connection::connect(&fake_options(port, extra)?, &Handle::current()).await
}

const fn script(tls: Tls, auth: Auth) -> Script {
    Script { tls, auth }
}

const PASSWORD_AUTHS: [Auth; 3] = [Auth::Cleartext, Auth::Md5, Auth::ScramWithoutPlus];

#[tokio::test]
async fn channel_binding_require_answers_no_password_request_in_clear() -> TestResult {
    for auth in PASSWORD_AUTHS {
        let server = FakeServer::start(vec![script(Tls::Refuse, auth)], None)?;
        let result = connect_fake(server.port(), "channel_binding=require").await;
        let Err(err) = result else {
            return Err(format!("{auth:?}: connect unexpectedly succeeded").into());
        };
        assert!(!err.to_string().contains("fake-secret-pw"), "{auth:?}");
        let [seen] = server
            .finished(1)?
            .try_into()
            .map_err(|_| "one connection")?;
        assert!(seen.startup_in_clear(), "{auth:?}: {seen:?}");
        assert_eq!(seen.after_auth_request, Reply::Closed, "{auth:?}: {seen:?}");
    }
    Ok(())
}

#[tokio::test]
async fn channel_binding_require_answers_no_password_request_under_tls() -> TestResult {
    let Some(certs) = common::test_certs()? else {
        return Ok(());
    };
    for auth in PASSWORD_AUTHS {
        let server = FakeServer::start(vec![script(Tls::Accept, auth)], Some(&certs))?;
        let result = connect_fake(server.port(), "sslmode=require channel_binding=require").await;
        assert!(result.is_err(), "{auth:?}");
        let [seen] = server
            .finished(1)?
            .try_into()
            .map_err(|_| "one connection")?;
        assert!(seen.startup_in_tls(), "{auth:?}: {seen:?}");
        assert_eq!(seen.after_auth_request, Reply::Closed, "{auth:?}: {seen:?}");
    }
    Ok(())
}

// the control: the same servers do get a response when channel binding is not required, so a zero
// above means the client held back and not that the fake server cannot see it
#[tokio::test]
async fn channel_binding_prefer_answers_the_password_request() -> TestResult {
    for auth in PASSWORD_AUTHS {
        let server = FakeServer::start(vec![script(Tls::Refuse, auth)], None)?;
        for setting in ["", "channel_binding=prefer", "channel_binding=disable"] {
            let _ = connect_fake(server.port(), setting).await;
        }
        let seen = server.finished(3)?;
        assert!(
            seen.iter()
                .all(|s| matches!(s.after_auth_request, Reply::Bytes(_))),
            "{auth:?}: {seen:?}"
        );
    }
    Ok(())
}

#[tokio::test]
async fn strict_sslmodes_never_fall_back_to_plaintext_when_the_server_answers_n() -> TestResult {
    let Some(certs) = common::test_certs()? else {
        return Ok(());
    };
    let root = certs.join("ca.crt");
    let verify_full = format!("sslmode=verify-full sslrootcert={}", root.display());
    for mode in ["sslmode=require", "sslmode=verify-ca", verify_full.as_str()] {
        let server = FakeServer::start(vec![script(Tls::Refuse, Auth::Ok)], None)?;
        let result = connect_fake(server.port(), mode).await;
        assert!(result.is_err(), "{mode}");
        let [seen] = server
            .finished(1)?
            .try_into()
            .map_err(|_| "one connection")?;
        assert!(seen.ssl_request, "{mode}: {seen:?}");
        assert!(
            !seen.startup_in_clear() && !seen.startup_in_tls(),
            "{mode}: {seen:?}"
        );
    }
    Ok(())
}

// the control for the test above: `prefer` does fall back, and so connects
#[tokio::test]
async fn sslmode_prefer_falls_back_to_plaintext_when_the_server_answers_n() -> TestResult {
    let server = FakeServer::start(vec![script(Tls::Refuse, Auth::Ok)], None)?;
    let mut conn = connect_fake(server.port(), "sslmode=prefer").await?;
    conn.close().await;
    let [seen] = server
        .finished(1)?
        .try_into()
        .map_err(|_| "one connection")?;
    assert!(seen.ssl_request && seen.startup_in_clear(), "{seen:?}");
    Ok(())
}

// a connection, then its cancel; the server reports the two in the order they finished
async fn connect_and_cancel(
    server: &FakeServer,
    extra: &str,
) -> Result<Result<(), Error>, Box<dyn std::error::Error>> {
    let mut conn = connect_fake(server.port(), extra).await?;
    let cancelled = conn.cancel_target().request().await;
    conn.close().await;
    Ok(cancelled)
}

#[tokio::test]
async fn cancel_with_sslmode_disable_is_sent_in_clear() -> TestResult {
    let server = FakeServer::start(vec![script(Tls::Refuse, Auth::Ok)], None)?;
    connect_and_cancel(&server, "sslmode=disable").await??;
    let seen = server.finished(2)?;
    let cancel = seen.iter().find(|s| s.cancel_in_clear());
    assert!(cancel.is_some_and(|s| !s.ssl_request), "{seen:?}");
    Ok(())
}

#[tokio::test]
async fn cancel_with_a_strict_sslmode_negotiates_tls_first() -> TestResult {
    let Some(certs) = common::test_certs()? else {
        return Ok(());
    };
    let root = certs.join("ca.crt");
    let verify_full = format!("sslmode=verify-full sslrootcert={}", root.display());
    for mode in ["sslmode=require", verify_full.as_str()] {
        let server = FakeServer::start(vec![script(Tls::Accept, Auth::Ok)], Some(&certs))?;
        connect_and_cancel(&server, mode).await??;
        let seen = server.finished(2)?;
        let cancel = seen.iter().find(|s| s.cancel_in_tls());
        assert!(cancel.is_some_and(|s| s.ssl_request), "{mode}: {seen:?}");
        assert!(
            seen.iter().all(|s| !s.cancel_in_clear()),
            "{mode}: {seen:?}"
        );
    }
    Ok(())
}

#[tokio::test]
async fn cancel_with_a_strict_sslmode_aborts_when_the_server_answers_n() -> TestResult {
    let Some(certs) = common::test_certs()? else {
        return Ok(());
    };
    let root = certs.join("ca.crt");
    let verify_full = format!("sslmode=verify-full sslrootcert={}", root.display());
    for mode in ["sslmode=require", verify_full.as_str()] {
        // the connection itself gets TLS; the second connection, the cancel, is refused it
        let server = FakeServer::start(
            vec![script(Tls::Accept, Auth::Ok), script(Tls::Refuse, Auth::Ok)],
            Some(&certs),
        )?;
        let cancelled = connect_and_cancel(&server, mode).await?;
        assert!(cancelled.is_err(), "{mode}");
        let seen = server.finished(2)?;
        assert!(
            seen.iter()
                .all(|s| !s.cancel_in_clear() && !s.cancel_in_tls()),
            "{mode}: {seen:?}"
        );
        assert!(
            seen.iter().any(|s| s.ssl_request && s.first.is_none()),
            "{mode}: {seen:?}"
        );
    }
    Ok(())
}

fn with_query(dsn: &str, extra: &str) -> String {
    let sep = if dsn.contains('?') { '&' } else { '?' };
    format!("{dsn}{sep}{extra}")
}

fn tls_dsn() -> TestResult<Option<String>> {
    if let Ok(dsn) = std::env::var("ROWX_TEST_DSN_SSL") {
        return Ok(Some(dsn));
    }
    if std::env::var("ROWX_REQUIRE_DB").is_ok_and(|v| v == "1") {
        return Err("ROWX_REQUIRE_DB=1 but ROWX_TEST_DSN_SSL is not set".into());
    }
    Ok(None)
}

#[tokio::test]
async fn scram_sha_256_plus_negotiates_against_the_tls_server() -> TestResult {
    let (Some(dsn), Some(certs)) = (tls_dsn()?, common::test_certs()?) else {
        return Ok(());
    };
    let handle = Handle::current();
    // a role that is certain to use SCRAM, whatever the server's pg_hba does for the test user
    let role = format!("rowx_cb_{}", std::process::id());
    let password = "scram-plus-pw";
    let mut admin = Connection::connect(
        &ConnectOptions::from_dsn(&with_query(&dsn, "sslmode=require"))?,
        &handle,
    )
    .await?;
    admin
        .client()?
        .batch_execute(&format!(
            "DROP ROLE IF EXISTS {role}; SET password_encryption = 'scram-sha-256'; \
             CREATE ROLE {role} LOGIN PASSWORD '{password}'"
        ))
        .await?;
    let stored = admin
        .client()?
        .query_one(
            "SELECT rolpassword LIKE 'SCRAM-SHA-256$%' FROM pg_catalog.pg_authid WHERE rolname = $1",
            &[&role],
        )
        .await?;
    assert!(stored.get::<_, bool>(0));
    let outcome = async {
        let base = ConnectOptions::from_dsn(&dsn)?;
        let ca = certs.join("ca.crt").display().to_string();
        let options = ConnectOptions {
            user: Some(role.clone()),
            password: Some(password.to_owned()),
            sslmode: Some("verify-full".to_owned()),
            sslrootcert: Some(ca),
            channel_binding: Some("require".to_owned()),
            ..base
        };
        let mut conn = Connection::connect(&options, &handle).await?;
        let row = conn
            .client()?
            .query_one(
                "SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()",
                &[],
            )
            .await?;
        let ssl: bool = row.get(0);
        conn.close().await;

        // the cancel request of a verified connection goes through TLS as well
        let mut again = Connection::connect(&options, &handle).await?;
        again.cancel_target().request().await?;
        again.close().await;
        Ok::<_, Box<dyn std::error::Error>>(ssl)
    }
    .await;
    admin
        .client()?
        .batch_execute(&format!("DROP ROLE {role}"))
        .await?;
    admin.close().await;
    assert!(outcome?);
    Ok(())
}

#[tokio::test]
async fn channel_binding_require_fails_against_a_server_without_tls() -> TestResult {
    let Some(dsn) = common::test_dsn()? else {
        return Ok(());
    };
    // sslmode=disable never reaches the network
    let Err(err) =
        ConnectOptions::from_dsn(&with_query(&dsn, "sslmode=disable&channel_binding=require"))
    else {
        return Err("require with sslmode=disable parsed".into());
    };
    assert!(
        matches!(&err, Error::Interface(m) if m.contains("channel_binding")),
        "{err:?}"
    );
    // sslmode=prefer (the default) parses, then no channel binding is available on a plain socket
    let options = ConnectOptions::from_dsn(&with_query(&dsn, "channel_binding=require"))?;
    let Err(err) = Connection::connect(&options, &Handle::current()).await else {
        return Err("connect without TLS succeeded under channel_binding=require".into());
    };
    // an authentication failure of the client's own making, not a server's rejection (which is `Db`)
    assert!(
        matches!(&err, Error::Interface(m) if m == "authentication error: server did not use channel binding"),
        "{err:?}"
    );
    Ok(())
}

fn root_option(certs: &std::path::Path) -> String {
    format!("sslrootcert={}", certs.join("ca.crt").display())
}

// connects to a one-script server and returns the failure with what the server saw of the connection
async fn refused(
    tls: Tls,
    auth: Auth,
    certs: Option<&std::path::Path>,
    extra: &str,
) -> TestResult<(Error, common::fake_server::Seen)> {
    let server = FakeServer::start(vec![script(tls, auth)], certs)?;
    let result = connect_fake(server.port(), extra).await;
    let Err(err) = result else {
        return Err(format!("{auth:?} {extra}: connect unexpectedly succeeded").into());
    };
    assert!(!err.to_string().contains("fake-secret-pw"), "{err}");
    let [seen] = server
        .finished(1)?
        .try_into()
        .map_err(|_| "one connection")?;
    Ok((err, seen))
}

#[tokio::test]
async fn channel_binding_require_names_the_refusal() -> TestResult {
    let (err, _) = refused(
        Tls::Refuse,
        Auth::Cleartext,
        None,
        "channel_binding=require",
    )
    .await?;
    assert_eq!(
        err,
        Error::Interface("authentication error: server did not use channel binding".to_owned())
    );
    Ok(())
}

#[tokio::test]
async fn channel_binding_require_rejects_authentication_ok_without_auth() -> TestResult {
    let (_, seen) = refused(Tls::Refuse, Auth::Ok, None, "channel_binding=require").await?;
    assert!(seen.startup_in_clear(), "{seen:?}");
    assert_eq!(seen.after_auth_request, Reply::Closed, "{seen:?}");
    let Some(certs) = common::test_certs()? else {
        return Ok(());
    };
    let (_, seen) = refused(
        Tls::Accept,
        Auth::Ok,
        Some(&certs),
        "sslmode=require channel_binding=require",
    )
    .await?;
    assert!(seen.startup_in_tls(), "{seen:?}");
    assert_eq!(seen.after_auth_request, Reply::Closed, "{seen:?}");
    Ok(())
}

#[tokio::test]
async fn channel_binding_require_rejects_scram_plus_over_plaintext() -> TestResult {
    let (_, seen) = refused(Tls::Refuse, Auth::OnlyPlus, None, "channel_binding=require").await?;
    assert!(seen.startup_in_clear(), "{seen:?}");
    assert_eq!(seen.after_auth_request, Reply::Closed, "{seen:?}");
    Ok(())
}

#[tokio::test]
async fn channel_binding_require_refuses_gss_and_sspi() -> TestResult {
    for auth in [Auth::Gss, Auth::Sspi] {
        let (_, seen) = refused(Tls::Refuse, auth, None, "channel_binding=require").await?;
        assert!(seen.startup_in_clear(), "{auth:?}: {seen:?}");
        assert_eq!(seen.after_auth_request, Reply::Closed, "{auth:?}: {seen:?}");
    }
    Ok(())
}

#[tokio::test]
async fn a_bad_scram_server_signature_fails_even_if_the_server_then_says_ok() -> TestResult {
    let Some(certs) = common::test_certs()? else {
        return Ok(());
    };
    let (err, seen) = refused(
        Tls::Accept,
        Auth::ScramBadFinal,
        Some(&certs),
        "sslmode=require channel_binding=require",
    )
    .await?;
    assert!(seen.startup_in_tls(), "{seen:?}");
    assert!(
        matches!(seen.after_auth_request, Reply::Bytes(_)),
        "{seen:?}"
    );
    assert!(
        matches!(&err, Error::Interface(m) if m.starts_with("authentication error")),
        "{err:?}"
    );
    Ok(())
}

#[tokio::test]
async fn verify_full_against_a_wrong_host_certificate_fails_before_the_startup_message()
-> TestResult {
    let Some(certs) = common::test_certs()? else {
        return Ok(());
    };
    if !certs.join("wrong-host.crt").exists() {
        return Err("scripts/gen-certs.sh did not generate wrong-host.crt".into());
    }
    let server = FakeServer::start_with_cert(
        vec![script(Tls::Accept, Auth::Ok)],
        Some(&certs),
        "wrong-host",
    )?;
    let extra = format!("sslmode=verify-full {}", root_option(&certs));
    assert!(connect_fake(server.port(), &extra).await.is_err());
    let [seen] = server
        .finished(1)?
        .try_into()
        .map_err(|_| "one connection")?;
    assert!(seen.ssl_request && seen.first.is_none(), "{seen:?}");
    Ok(())
}

#[tokio::test]
async fn s_followed_by_plaintext_never_falls_back_to_plaintext() -> TestResult {
    let Some(certs) = common::test_certs()? else {
        return Ok(());
    };
    let verify_full = format!("sslmode=verify-full {}", root_option(&certs));
    for mode in [verify_full.as_str(), "sslmode=require", "sslmode=prefer"] {
        let (_, seen) = refused(Tls::AcceptThenPlaintext, Auth::Ok, None, mode).await?;
        assert!(seen.ssl_request, "{mode}: {seen:?}");
        assert!(
            !seen.startup_in_clear() && !seen.startup_in_tls(),
            "{mode}: {seen:?}"
        );
    }
    Ok(())
}
