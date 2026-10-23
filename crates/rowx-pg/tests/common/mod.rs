// clippy::redundant_pub_crate rejects pub(crate) in a private module, so rustc's unreachable_pub is silenced here
#![allow(unreachable_pub)]

use std::collections::hash_map::RandomState;
use std::hash::{BuildHasher, Hasher};

use tokio_postgres::{Client, Config, NoTls};

const SCHEMA_SQL: &str = include_str!("../../../../tests/schema.sql");
const SEED_SQL: &str = include_str!("../../../../tests/seed_data.sql");

pub type TestResult<T = ()> = Result<T, Box<dyn std::error::Error>>;

/// `ROWX_TEST_DSN`, or `None` when DB tests should skip. Fails when `ROWX_REQUIRE_DB=1` and the DSN is unset.
pub fn test_dsn() -> TestResult<Option<String>> {
    if let Ok(dsn) = std::env::var("ROWX_TEST_DSN") {
        return Ok(Some(dsn));
    }
    if std::env::var("ROWX_REQUIRE_DB").is_ok_and(|v| v == "1") {
        return Err("ROWX_REQUIRE_DB=1 but ROWX_TEST_DSN is not set".into());
    }
    Ok(None)
}

pub struct Schema {
    pub name: String,
    pub dsn: String,
    admin_dsn: String,
}

impl Schema {
    pub async fn connect(&self) -> TestResult<Client> {
        connect(&self.dsn).await
    }

    pub async fn drop(self) -> TestResult {
        let admin = connect(&self.admin_dsn).await?;
        admin
            .batch_execute(&format!("DROP SCHEMA \"{}\" CASCADE", self.name))
            .await?;
        Ok(())
    }
}

async fn connect(dsn: &str) -> TestResult<Client> {
    let config: Config = dsn.parse()?;
    let (client, connection) = config.connect(NoTls).await?;
    tokio::spawn(connection);
    Ok(client)
}

fn random_hex() -> String {
    let a = RandomState::new().build_hasher().finish();
    let b = RandomState::new().build_hasher().finish();
    format!("{a:016x}{b:016x}")
}

/// Creates a schema `t_<hex>` from `tests/schema.sql` plus the seed data. Returns `None` when DB tests skip.
pub async fn fresh_schema() -> TestResult<Option<Schema>> {
    let Some(admin_dsn) = test_dsn()? else {
        return Ok(None);
    };
    let name = format!("t_{}", &random_hex()[..16]);
    let admin = connect(&admin_dsn).await?;
    admin
        .batch_execute(&format!("CREATE SCHEMA \"{name}\""))
        .await?;
    // citext lives in public and is shared, so concurrent loads must not race creating it
    let setup = format!(
        "BEGIN; SELECT pg_advisory_xact_lock(727); SET LOCAL search_path = \"{name}\"; {SCHEMA_SQL}\n{SEED_SQL}\nCOMMIT;"
    );
    admin.batch_execute(&setup).await?;

    // the DSN carries search_path through the libpq `options` parameter
    let dsn = config_to_url(&admin_dsn, &name);
    Ok(Some(Schema {
        name,
        dsn,
        admin_dsn,
    }))
}

fn config_to_url(admin_dsn: &str, schema: &str) -> String {
    let sep = if admin_dsn.contains('?') { '&' } else { '?' };
    format!("{admin_dsn}{sep}options=-c%20search_path%3D{schema}")
}
