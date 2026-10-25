// pub, so the shared helpers are reachable from the crate root and need no visibility waiver
pub mod common;

use common::TestResult;
use rowx_postgres::error::Error;
use rowx_postgres::exec::{self, Batch, Encode, Param, RowBatches, Shape};
use rowx_postgres::stmt_cache::StmtCache;
use rowx_postgres::{Row, Type};
use tokio_postgres::{Client, Statement};

fn no_params() -> impl Encode {
    |_: &[Type]| -> Result<Vec<Param>, Error> { Ok(Vec::new()) }
}

fn cache() -> StmtCache<Statement> {
    StmtCache::new(16)
}

async fn open_stream(
    client: &Client,
    cache: &mut StmtCache<Statement>,
    sql: &str,
    mask: Option<&[bool]>,
    batch_size: usize,
) -> Result<RowBatches, Error> {
    let shape = Shape {
        mask,
        ..Shape::default()
    };
    exec::stream(client, cache, sql, &shape, no_params(), batch_size).await
}

// every batch until the end, as (batch sizes, rows, rows_affected); stops at the first error
async fn drain(batches: &mut RowBatches) -> (Vec<usize>, Vec<Row>, Result<Option<u64>, Error>) {
    let (mut sizes, mut all) = (Vec::new(), Vec::new());
    loop {
        match batches.next_batch().await {
            Ok(Batch::Rows(rows)) => {
                sizes.push(rows.len());
                all.extend(rows);
            }
            Ok(Batch::End { rows_affected }) => return (sizes, all, Ok(rows_affected)),
            Err(err) => return (sizes, all, Err(err)),
        }
    }
}

#[tokio::test]
async fn streams_in_batches_bounded_by_batch_size() -> TestResult {
    let Some(schema) = common::fresh_schema().await? else {
        return Ok(());
    };
    let client = schema.connect().await?;
    let mut cache = cache();
    let sql = "SELECT g::int4 FROM generate_series(1, 25) g";

    let mut batches = open_stream(&client, &mut cache, sql, None, 4).await?;
    let (sizes, rows, end) = drain(&mut batches).await;
    assert!(sizes.iter().all(|n| (1..=4).contains(n)), "{sizes:?}");
    let values: Vec<i32> = rows.iter().map(|r| r.get(0)).collect();
    assert_eq!(values, (1..=25).collect::<Vec<_>>());
    assert_eq!(end?, Some(25));

    let mut batches = open_stream(&client, &mut cache, sql, None, 1).await?;
    let (sizes, _, _) = drain(&mut batches).await;
    assert_eq!(sizes, vec![1; 25]);

    schema.drop().await
}

#[tokio::test]
async fn rows_affected_after_insert_returning() -> TestResult {
    let Some(schema) = common::fresh_schema().await? else {
        return Ok(());
    };
    let client = schema.connect().await?;
    let mut cache = cache();
    client.batch_execute("CREATE TABLE ins (v int4)").await?;

    let sql = "INSERT INTO ins SELECT g FROM generate_series(1, 7) g RETURNING v";
    let mut batches = open_stream(&client, &mut cache, sql, None, 3).await?;
    let (_, rows, end) = drain(&mut batches).await;
    assert_eq!(rows.len(), 7);
    assert_eq!(end?, Some(7));

    schema.drop().await
}

#[tokio::test]
async fn rows_affected_after_select() -> TestResult {
    let Some(schema) = common::fresh_schema().await? else {
        return Ok(());
    };
    let client = schema.connect().await?;
    let mut cache = cache();

    let mut batches = open_stream(&client, &mut cache, "SELECT id FROM users", None, 10).await?;
    let (_, rows, end) = drain(&mut batches).await;
    assert_eq!(rows.len(), 3);
    assert_eq!(end?, Some(3));

    schema.drop().await
}

#[tokio::test]
async fn null_mask_violation_midstream_yields_prefix_then_error() -> TestResult {
    let Some(schema) = common::fresh_schema().await? else {
        return Ok(());
    };
    let client = schema.connect().await?;
    let mut cache = cache();
    let sql = "SELECT CASE WHEN g = 3 THEN NULL ELSE g END::int4 FROM generate_series(1, 5) g";

    let mut batches = open_stream(&client, &mut cache, sql, Some(&[false]), 100).await?;
    let (_, rows, end) = drain(&mut batches).await;
    let values: Vec<i32> = rows.iter().map(|r| r.get(0)).collect();
    assert_eq!(values, vec![1, 2]);
    assert!(matches!(end, Err(Error::RowShape(_))), "{end:?}");

    // the connection is usable and the stream stays ended
    assert!(matches!(
        batches.next_batch().await?,
        Batch::End {
            rows_affected: None
        }
    ));
    assert_eq!(
        client
            .query_one("SELECT 1::int4", &[])
            .await?
            .get::<_, i32>(0),
        1
    );

    schema.drop().await
}

#[tokio::test]
async fn schema_change_retry_before_first_batch() -> TestResult {
    let Some(schema) = common::fresh_schema().await? else {
        return Ok(());
    };
    let client = schema.connect().await?;
    let other = schema.connect().await?;
    let mut cache = cache();
    client
        .batch_execute("CREATE TABLE sc (v int4); INSERT INTO sc VALUES (1), (2)")
        .await?;

    let sql = "SELECT v FROM sc ORDER BY v";
    let mut batches = open_stream(&client, &mut cache, sql, None, 10).await?;
    let (_, rows, _) = drain(&mut batches).await;
    assert_eq!(rows.len(), 2);

    // the cached statement now describes int4 while the server returns int8
    other
        .batch_execute("ALTER TABLE sc ALTER COLUMN v TYPE int8")
        .await?;

    let mut batches = open_stream(&client, &mut cache, sql, None, 10).await?;
    let (_, rows, end) = drain(&mut batches).await;
    let values: Vec<i64> = rows.iter().map(|r| r.get(0)).collect();
    assert_eq!(values, vec![1, 2]);
    assert_eq!(end?, Some(2));

    schema.drop().await
}

#[tokio::test]
async fn dropping_stream_early_leaves_client_usable_and_rows_committed() -> TestResult {
    let Some(schema) = common::fresh_schema().await? else {
        return Ok(());
    };
    let client = schema.connect().await?;
    let mut cache = cache();
    client.batch_execute("CREATE TABLE ins (v int4)").await?;

    let sql = "INSERT INTO ins SELECT g FROM generate_series(1, 5000) g RETURNING v";
    let mut batches = open_stream(&client, &mut cache, sql, None, 10).await?;
    let Batch::Rows(first) = batches.next_batch().await? else {
        return Err("expected rows".into());
    };
    assert!(!first.is_empty());
    drop(batches);

    let count: i64 = client
        .query_one("SELECT count(*) FROM ins", &[])
        .await?
        .get(0);
    assert_eq!(count, 5000);

    schema.drop().await
}

#[tokio::test]
async fn fetch_collects_all_rows() -> TestResult {
    let Some(schema) = common::fresh_schema().await? else {
        return Ok(());
    };
    let client = schema.connect().await?;
    let mut cache = cache();

    let rows = exec::fetch(
        &client,
        &mut cache,
        "SELECT id FROM users",
        &Shape::default(),
        no_params(),
    )
    .await?;
    assert_eq!(rows.len(), 3);

    schema.drop().await
}

#[tokio::test]
async fn fetch_limit_one_returns_first_row() -> TestResult {
    let Some(schema) = common::fresh_schema().await? else {
        return Ok(());
    };
    let client = schema.connect().await?;
    let mut cache = cache();
    let shape = Shape {
        limit_one: true,
        ..Shape::default()
    };

    let rows = exec::fetch(
        &client,
        &mut cache,
        "SELECT id FROM users ORDER BY id",
        &shape,
        no_params(),
    )
    .await?;
    assert_eq!(rows.len(), 1);
    assert_eq!(
        client
            .query_one("SELECT 1::int4", &[])
            .await?
            .get::<_, i32>(0),
        1
    );

    schema.drop().await
}

#[tokio::test]
async fn execute_returns_rows_affected() -> TestResult {
    let Some(schema) = common::fresh_schema().await? else {
        return Ok(());
    };
    let client = schema.connect().await?;
    let mut cache = cache();
    client.batch_execute("CREATE TABLE ins (v int4)").await?;

    let n = exec::execute(
        &client,
        &mut cache,
        "INSERT INTO ins SELECT g FROM generate_series(1, 4) g",
        no_params(),
    )
    .await?;
    assert_eq!(n, 4);

    schema.drop().await
}

#[tokio::test]
async fn db_error_midstream_yields_prefix_then_error() -> TestResult {
    let Some(schema) = common::fresh_schema().await? else {
        return Ok(());
    };
    let client = schema.connect().await?;
    let mut cache = cache();
    let sql = "SELECT (10 / (3 - g))::int4 FROM generate_series(1, 5) g";

    let mut batches = open_stream(&client, &mut cache, sql, None, 100).await?;
    let (_, rows, end) = drain(&mut batches).await;
    assert_eq!(rows.len(), 2);
    assert!(
        matches!(&end, Err(Error::Db { sqlstate, .. }) if &**sqlstate == "22012"),
        "{end:?}"
    );
    assert_eq!(
        client
            .query_one("SELECT 1::int4", &[])
            .await?
            .get::<_, i32>(0),
        1
    );

    schema.drop().await
}
