pub mod common;

use std::collections::hash_map::RandomState;
use std::hash::{BuildHasher, Hasher};

use common::TestResult;
use rowx_postgres::admin::{create_database, database_exists, drop_database, list_databases};
use rowx_postgres::conn::Connection;
use rowx_postgres::dsn::ConnectOptions;
use rowx_postgres::error::Error;
use tokio::runtime::Handle;

fn unique(prefix: &str) -> String {
    format!(
        "{prefix}_{:016x}",
        RandomState::new().build_hasher().finish()
    )
}

fn options(dsn: &str) -> Result<ConnectOptions, Error> {
    ConnectOptions::from_dsn(dsn)
}

fn on(base: &ConnectOptions, database: &str) -> ConnectOptions {
    ConnectOptions {
        database: Some(database.to_owned()),
        ..base.clone()
    }
}

async fn open(base: &ConnectOptions, database: &str) -> Result<Connection, Error> {
    Connection::connect(&on(base, database), &Handle::current()).await
}

fn sqlstate(err: &Error) -> Option<&str> {
    match err {
        Error::Db { sqlstate, .. } => Some(sqlstate),
        _ => None,
    }
}

#[tokio::test]
async fn create_exists_and_drop() -> TestResult {
    let Some(dsn) = common::test_dsn()? else {
        return Ok(());
    };
    let base = options(&dsn)?;
    let handle = Handle::current();
    let name = unique("rowx_admin");

    assert!(!database_exists(&base, &name, &handle).await?);
    create_database(&base, &name, None, None, &handle).await?;
    assert!(database_exists(&base, &name, &handle).await?);

    let mut conn = open(&base, &name).await?;
    conn.close().await;

    let again = create_database(&base, &name, None, None, &handle).await;
    assert_eq!(again.as_ref().err().and_then(sqlstate), Some("42P04"));

    drop_database(&base, &name, false, &handle).await?;
    assert!(!database_exists(&base, &name, &handle).await?);
    // a missing database is not an error
    drop_database(&base, &name, false, &handle).await?;
    Ok(())
}

#[tokio::test]
async fn creates_from_a_template() -> TestResult {
    let Some(dsn) = common::test_dsn()? else {
        return Ok(());
    };
    let base = options(&dsn)?;
    let handle = Handle::current();
    let template = unique("rowx_tpl");
    let clone = unique("rowx_clone");

    create_database(&base, &template, None, None, &handle).await?;
    let mut conn = open(&base, &template).await?;
    conn.simple("CREATE TABLE marker (i int); INSERT INTO marker VALUES (42)")
        .await?;
    conn.close().await;

    create_database(&base, &clone, Some(&template), None, &handle).await?;
    let mut conn = open(&base, &clone).await?;
    let row = conn
        .client()?
        .query_one("SELECT i FROM marker", &[])
        .await?;
    assert_eq!(row.get::<_, i32>(0), 42);
    conn.close().await;

    drop_database(&base, &clone, false, &handle).await?;
    drop_database(&base, &template, false, &handle).await?;
    Ok(())
}

#[tokio::test]
async fn quotes_names() -> TestResult {
    let Some(dsn) = common::test_dsn()? else {
        return Ok(());
    };
    let base = options(&dsn)?;
    let handle = Handle::current();
    let suffix = unique("q");
    let names = [
        format!("a-b {suffix}"),
        format!("a\"b {suffix}"),
        format!("Mixed;Case {suffix}"),
    ];
    for name in &names {
        create_database(&base, name, None, None, &handle).await?;
        assert!(database_exists(&base, name, &handle).await?);
        let mut conn = open(&base, name).await?;
        let row = conn
            .client()?
            .query_one("SELECT current_database()", &[])
            .await?;
        assert_eq!(row.get::<_, String>(0), *name);
        conn.close().await;
        drop_database(&base, name, false, &handle).await?;
        assert!(!database_exists(&base, name, &handle).await?);
    }
    Ok(())
}

#[tokio::test]
async fn rejects_unusable_names() -> TestResult {
    let Some(dsn) = common::test_dsn()? else {
        return Ok(());
    };
    let base = options(&dsn)?;
    let handle = Handle::current();
    assert!(matches!(
        database_exists(&base, "", &handle).await,
        Err(Error::Interface(_))
    ));
    assert!(matches!(
        create_database(&base, "a\0b", None, None, &handle).await,
        Err(Error::Interface(_))
    ));
    // the server would truncate it, so the lookup by the full name would miss
    assert!(matches!(
        drop_database(&base, &"a".repeat(64), false, &handle).await,
        Err(Error::Interface(_))
    ));
    Ok(())
}

#[tokio::test]
async fn force_drops_a_database_with_a_live_session() -> TestResult {
    let Some(dsn) = common::test_dsn()? else {
        return Ok(());
    };
    let base = options(&dsn)?;
    let handle = Handle::current();
    let name = unique("rowx_force");
    create_database(&base, &name, None, None, &handle).await?;
    let mut session = open(&base, &name).await?;

    let blocked = drop_database(&base, &name, false, &handle).await;
    assert_eq!(blocked.as_ref().err().and_then(sqlstate), Some("55006"));
    assert!(database_exists(&base, &name, &handle).await?);

    drop_database(&base, &name, true, &handle).await?;
    assert!(!database_exists(&base, &name, &handle).await?);
    // the terminated session is dead
    let dead = match session.client() {
        Ok(client) => client.simple_query("SELECT 1").await.is_err(),
        Err(_) => true,
    };
    assert!(dead);
    session.close().await;
    Ok(())
}

#[tokio::test]
async fn maintenance_database_switches_for_a_target_named_postgres() -> TestResult {
    let Some(dsn) = common::test_dsn()? else {
        return Ok(());
    };
    let base = options(&dsn)?;
    let handle = Handle::current();
    // the target `postgres` exists, and a session on it must not block the check, because the
    // lookup runs on template1 instead
    assert!(database_exists(&base, "postgres", &handle).await?);
    let mut session = open(&base, "postgres").await?;
    // dropping it needs template1, so the call reaches the server and is refused for the open session
    // (or, without privilege, for ownership) instead of failing to connect
    let refused = drop_database(&base, "postgres", false, &handle).await;
    let err = refused.err().ok_or("dropping postgres must fail")?;
    // connecting to `postgres` itself would be refused as "cannot drop the currently open database"
    match &err {
        Error::Db { message, .. } => assert!(!message.contains("currently open"), "{err:?}"),
        _ => return Err(format!("expected a server refusal, got {err:?}").into()),
    }
    assert!(database_exists(&base, "postgres", &handle).await?);
    session.close().await;
    Ok(())
}

#[tokio::test]
async fn errors_never_contain_the_password() -> TestResult {
    let Some(dsn) = common::test_dsn()? else {
        return Ok(());
    };
    let secret = "hunter2-not-the-password";
    let bad = ConnectOptions {
        password: Some(secret.to_owned()),
        ..options(&dsn)?
    };
    let err = database_exists(&bad, "anything", &Handle::current())
        .await
        .err()
        .ok_or("a wrong password must fail")?;
    assert!(!format!("{err:?} {err}").contains(secret));
    Ok(())
}

#[tokio::test]
async fn a_comment_round_trips_through_list_databases() -> TestResult {
    let Some(dsn) = common::test_dsn()? else {
        return Ok(());
    };
    let base = options(&dsn)?;
    let handle = Handle::current();
    let prefix = unique("rowx_cmt");
    let tagged = format!("{prefix}_tagged");
    let plain = format!("{prefix}_plain");
    let tag = "rowx:42@host's\\name:1700000000";

    create_database(&base, &tagged, None, Some(tag), &handle).await?;
    let outcome = async {
        create_database(&base, &plain, None, None, &handle).await?;
        let mut session = open(&base, &plain).await?;
        let listed = list_databases(&base, &prefix, &handle).await;
        session.close().await;
        let listed = listed?;
        let names: Vec<_> = listed.iter().map(|d| d.name.as_str()).collect();
        assert_eq!(names, [plain.as_str(), tagged.as_str()]);
        assert_eq!(listed[0].comment, None);
        assert_eq!(listed[0].sessions, 1);
        assert_eq!(listed[1].comment.as_deref(), Some(tag));
        assert_eq!(listed[1].sessions, 0);
        // a prefix is not a pattern
        assert_eq!(list_databases(&base, "rowx%", &handle).await?.len(), 0);
        TestResult::Ok(())
    }
    .await;
    let _ = drop_database(&base, &tagged, true, &handle).await;
    let _ = drop_database(&base, &plain, true, &handle).await;
    outcome
}

#[tokio::test]
async fn postgres_falls_back_to_template1_when_connect_is_denied() -> TestResult {
    let Some(dsn) = common::test_dsn()? else {
        return Ok(());
    };
    let admin = options(&dsn)?;
    let handle = Handle::current();
    let role = unique("rowx_nocon");
    let target = unique("rowx_fallback");
    let password = "rowx-nocon";

    let mut setup = open(&admin, "postgres").await?;
    let outcome = async {
        setup
            .simple(&format!(
                "CREATE ROLE \"{role}\" LOGIN CREATEDB PASSWORD '{password}'; \
                 REVOKE CONNECT ON DATABASE postgres FROM PUBLIC"
            ))
            .await?;
        // the URL's database is the target, so `postgres` is the first choice, and the role may not enter it
        let denied = ConnectOptions {
            user: Some(role.clone()),
            password: Some(password.to_owned()),
            database: Some(target.clone()),
            ..admin.clone()
        };
        let direct = Connection::connect(&on(&denied, "postgres"), &handle).await;
        assert_eq!(direct.as_ref().err().and_then(sqlstate), Some("42501"));

        create_database(&denied, &target, None, Some("fell back"), &handle).await?;
        assert!(database_exists(&denied, &target, &handle).await?);
        let listed = list_databases(&admin, &target, &handle).await?;
        assert_eq!(listed.len(), 1);
        assert_eq!(listed[0].comment.as_deref(), Some("fell back"));
        drop_database(&denied, &target, false, &handle).await?;
        assert!(!database_exists(&denied, &target, &handle).await?);
        TestResult::Ok(())
    }
    .await;
    // restored whatever happened above
    let restore = setup
        .simple("GRANT CONNECT ON DATABASE postgres TO PUBLIC")
        .await;
    let _ = drop_database(&admin, &target, true, &handle).await;
    let _ = setup
        .simple(&format!("DROP ROLE IF EXISTS \"{role}\""))
        .await;
    setup.close().await;
    restore?;
    outcome
}

#[tokio::test]
async fn a_forced_drop_without_privilege_explains_what_is_needed() -> TestResult {
    let Some(dsn) = common::test_dsn()? else {
        return Ok(());
    };
    let admin = options(&dsn)?;
    let handle = Handle::current();
    let role = unique("rowx_noforce");
    let name = unique("rowx_forced");
    let password = "rowx-noforce";

    let mut setup = open(&admin, "postgres").await?;
    let outcome = async {
        setup
            .simple(&format!(
                "CREATE ROLE \"{role}\" LOGIN PASSWORD '{password}'"
            ))
            .await?;
        create_database(&admin, &name, None, None, &handle).await?;
        let weak = ConnectOptions {
            user: Some(role.clone()),
            password: Some(password.to_owned()),
            ..admin.clone()
        };
        let refused = drop_database(&weak, &name, true, &handle).await;
        match refused
            .err()
            .ok_or("a role that doesn't own it must fail")?
        {
            Error::Db { sqlstate, hint, .. } => {
                assert_eq!(&*sqlstate, "42501");
                assert!(hint.is_some_and(|h| h.contains("pg_signal_backend")));
            }
            other => return Err(format!("expected a server refusal, got {other:?}").into()),
        }
        assert!(database_exists(&admin, &name, &handle).await?);
        TestResult::Ok(())
    }
    .await;
    let _ = drop_database(&admin, &name, true, &handle).await;
    let _ = setup
        .simple(&format!("DROP ROLE IF EXISTS \"{role}\""))
        .await;
    setup.close().await;
    outcome
}
