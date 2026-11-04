// pub, so the shared helpers are reachable from the crate root and need no visibility waiver
pub mod common;

use common::TestResult;
use rowx_core::row_shape::{FieldSpec, Mapping, Matching, Names, ShapeError};
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

    let (rows, _) = exec::fetch(
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

    let (rows, _) = exec::fetch(
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

fn field(name: &str, required: bool) -> FieldSpec {
    FieldSpec {
        names: Names::Known(vec![name.to_owned()]),
        required,
    }
}

// a field with unknown names, which lets unmatched columns pass through
const fn open_names() -> FieldSpec {
    FieldSpec {
        names: Names::Unknown,
        required: false,
    }
}

fn named<'a>(fields: &'a [FieldSpec], mask: &'a [bool]) -> Shape<'a> {
    Shape {
        mask: Some(mask),
        fields: Some(fields),
        matching: Matching::ByName,
        ..Shape::default()
    }
}

fn shape_errors(err: Error) -> TestResult<Vec<ShapeError>> {
    match err {
        Error::Shape { errors, .. } => Ok(errors.into_iter().collect()),
        other => Err(Box::new(other)),
    }
}

#[tokio::test]
async fn named_mapping_error_is_reported_on_an_empty_result() -> TestResult {
    let Some(schema) = common::fresh_schema().await? else {
        return Ok(());
    };
    let client = schema.connect().await?;
    let mut cache = cache();
    let fields = [field("id", true), field("name", true)];
    let mask = [false, false];
    let sql = "SELECT 1::int4 AS id, 2::int4 AS age WHERE false";
    let expected = vec![
        ShapeError::UnexpectedColumn {
            index: 1,
            name: "age".into(),
        },
        ShapeError::MissingColumn { field: 1 },
    ];

    let fetched = exec::fetch(
        &client,
        &mut cache,
        sql,
        &named(&fields, &mask),
        no_params(),
    )
    .await;
    assert_eq!(shape_errors(fetched.err().ok_or("no error")?)?, expected);
    let opened = exec::stream(
        &client,
        &mut cache,
        sql,
        &named(&fields, &mask),
        no_params(),
        10,
    )
    .await;
    assert_eq!(shape_errors(opened.err().ok_or("no error")?)?, expected);

    schema.drop().await
}

#[tokio::test]
async fn named_mapping_reprepares_a_cached_statement_after_a_column_is_added() -> TestResult {
    let Some(schema) = common::fresh_schema().await? else {
        return Ok(());
    };
    let client = schema.connect().await?;
    let mut cache = cache();
    client
        .batch_execute("CREATE TABLE grows (a int4); INSERT INTO grows VALUES (1)")
        .await?;
    let sql = "SELECT * FROM grows";

    let one = [field("a", true)];
    let (rows, mapping) = exec::fetch(
        &client,
        &mut cache,
        sql,
        &named(&one, &[false]),
        no_params(),
    )
    .await?;
    assert_eq!(rows.len(), 1);
    assert_eq!(
        mapping,
        Some(Mapping {
            columns: vec![Some(0)],
            absent: vec![]
        })
    );

    client
        .batch_execute("ALTER TABLE grows ADD COLUMN b int4 DEFAULT 7")
        .await?;
    let two = [field("a", true), field("b", true)];
    let (rows, mapping) = exec::fetch(
        &client,
        &mut cache,
        sql,
        &named(&two, &[false, false]),
        no_params(),
    )
    .await?;
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0].get::<_, i32>(1), 7);
    assert_eq!(
        mapping,
        Some(Mapping {
            columns: vec![Some(0), Some(1)],
            absent: vec![]
        })
    );

    // a real mismatch is still reported once the statement is fresh
    let fetched = exec::fetch(
        &client,
        &mut cache,
        sql,
        &named(&one, &[false]),
        no_params(),
    )
    .await;
    assert_eq!(
        shape_errors(fetched.err().ok_or("no error")?)?,
        vec![ShapeError::UnexpectedColumn {
            index: 1,
            name: "b".into()
        }]
    );

    schema.drop().await
}

#[tokio::test]
async fn named_fetch_rejects_null_in_a_non_nullable_field() -> TestResult {
    let Some(schema) = common::fresh_schema().await? else {
        return Ok(());
    };
    let client = schema.connect().await?;
    let mut cache = cache();
    // fields are declared in the opposite order to the columns, and `extra` passes through
    let fields = [field("b", true), field("a", true), open_names()];
    let sql = "SELECT 1::int4 AS a, NULL::int4 AS b, NULL::int4 AS extra";

    let fetched = exec::fetch(
        &client,
        &mut cache,
        sql,
        &Shape {
            fields: Some(&fields),
            mask: Some(&[false, true, true]),
            ..Shape::default()
        },
        no_params(),
    )
    .await;
    assert_eq!(
        fetched.err().ok_or("no error")?,
        Error::NullField {
            column: 1,
            field: 0,
            row: 0,
            name: "b".into()
        }
    );

    // a nullable field and a pass-through column accept NULL
    let (rows, mapping) = exec::fetch(
        &client,
        &mut cache,
        sql,
        &named(&fields, &[true, false, true]),
        no_params(),
    )
    .await?;
    assert_eq!(rows.len(), 1);
    assert_eq!(
        mapping.map(|m| m.columns),
        Some(vec![Some(1), Some(0), None])
    );

    schema.drop().await
}

#[tokio::test]
async fn named_stream_rejects_null_after_the_rows_before_it() -> TestResult {
    let Some(schema) = common::fresh_schema().await? else {
        return Ok(());
    };
    let client = schema.connect().await?;
    let mut cache = cache();
    let fields = [field("v", true), open_names()];
    let sql = "SELECT CASE WHEN g = 3 THEN NULL ELSE g END::int4 AS v, NULL::int4 AS extra \
               FROM generate_series(1, 5) g";

    let mut batches = exec::stream(
        &client,
        &mut cache,
        sql,
        &named(&fields, &[false, true]),
        no_params(),
        100,
    )
    .await?;
    assert_eq!(
        batches.mapping().map(|m| m.columns.clone()),
        Some(vec![Some(0), None])
    );
    let (_, rows, end) = drain(&mut batches).await;
    assert_eq!(rows.len(), 2);
    assert_eq!(
        end.err().ok_or("no error")?,
        Error::NullField {
            column: 0,
            field: 0,
            row: 2,
            name: "v".into()
        }
    );

    schema.drop().await
}

#[tokio::test]
async fn named_shape_without_a_full_field_mask_is_an_interface_error() -> TestResult {
    let Some(schema) = common::fresh_schema().await? else {
        return Ok(());
    };
    let client = schema.connect().await?;
    let mut cache = cache();
    let fields = [field("a", true), field("b", true)];
    let sql = "SELECT 1::int4 AS a, NULL::int4 AS b";

    for mask in [Some(&[false][..]), None] {
        let shape = Shape {
            mask,
            fields: Some(&fields),
            ..Shape::default()
        };
        let fetched = exec::fetch(&client, &mut cache, sql, &shape, no_params()).await;
        assert!(matches!(fetched.err(), Some(Error::Interface(_))));
        let opened = exec::stream(&client, &mut cache, sql, &shape, no_params(), 10).await;
        assert!(matches!(opened.err(), Some(Error::Interface(_))));
    }

    schema.drop().await
}

#[tokio::test]
async fn unnamed_shapes_have_no_mapping() -> TestResult {
    let Some(schema) = common::fresh_schema().await? else {
        return Ok(());
    };
    let client = schema.connect().await?;
    let mut cache = cache();
    let sql = "SELECT 1::int4 AS a";
    let shape = Shape {
        arity: Some(1),
        scalar: true,
        ..Shape::default()
    };

    let (rows, mapping) = exec::fetch(&client, &mut cache, sql, &shape, no_params()).await?;
    assert_eq!(rows.len(), 1);
    assert_eq!(mapping, None);
    let batches = exec::stream(&client, &mut cache, sql, &shape, no_params(), 10).await?;
    assert_eq!(batches.mapping(), None);

    schema.drop().await
}

#[tokio::test]
async fn named_stream_accepts_null_in_a_pass_through_column() -> TestResult {
    let Some(schema) = common::fresh_schema().await? else {
        return Ok(());
    };
    let client = schema.connect().await?;
    let mut cache = cache();
    let fields = [field("v", true), open_names()];
    let sql = "SELECT g::int4 AS v, NULL::int4 AS extra FROM generate_series(1, 3) g";

    let mut batches = exec::stream(
        &client,
        &mut cache,
        sql,
        &named(&fields, &[false, false]),
        no_params(),
        100,
    )
    .await?;
    let (_, rows, end) = drain(&mut batches).await;
    assert_eq!(rows.len(), 3);
    assert_eq!(end?, Some(3));

    schema.drop().await
}

async fn assert_connection_usable(client: &Client) -> TestResult {
    assert_eq!(
        client
            .query_one("SELECT 1::int4", &[])
            .await?
            .get::<_, i32>(0),
        1
    );
    Ok(())
}

// RUSTSEC-2024-0363 class: a length that overflows the protocol's i32 or u16 must fail before
// anything is written, and the connection must stay in sync
#[tokio::test]
#[ignore = "needs ~2 GiB of memory; runs in the scheduled fuzz workflow"]
async fn parameter_of_2_pow_31_bytes_errors_and_keeps_connection() -> TestResult {
    let Some(schema) = common::fresh_schema().await? else {
        return Ok(());
    };
    let client = schema.connect().await?;
    let mut cache = cache();

    // one allocation; zeroed pages stay untouched until the driver copies them into the Bind buffer
    let huge = Param::binary(vec![0_u8; 1 << 31]);
    let encode = move |_: &[Type]| -> Result<Vec<Param>, Error> { Ok(vec![huge.clone()]) };
    let err = exec::execute(&client, &mut cache, "SELECT length($1::bytea)", encode)
        .await
        .err();
    assert!(
        matches!(err, Some(Error::Interface(_) | Error::Encode(_))),
        "{err:?}"
    );
    assert_connection_usable(&client).await?;

    schema.drop().await
}

#[tokio::test]
async fn sql_text_with_nul_errors_and_keeps_connection() -> TestResult {
    let Some(schema) = common::fresh_schema().await? else {
        return Ok(());
    };
    let client = schema.connect().await?;
    let mut cache = cache();

    let err = exec::execute(&client, &mut cache, "SELECT 1\0", no_params())
        .await
        .err();
    assert!(
        matches!(err, Some(Error::Interface(_) | Error::Encode(_))),
        "{err:?}"
    );
    assert_connection_usable(&client).await?;

    schema.drop().await
}

#[tokio::test]
async fn more_than_65535_parameters_error_and_keep_connection() -> TestResult {
    let Some(schema) = common::fresh_schema().await? else {
        return Ok(());
    };
    let client = schema.connect().await?;
    let mut cache = cache();

    let count = usize::from(u16::MAX) + 1;
    let placeholders: Vec<String> = (1..=count).map(|n| format!("${n}::int4")).collect();
    let sql = format!("SELECT cardinality(ARRAY[{}])", placeholders.join(", "));
    let encode = |_: &[Type]| -> Result<Vec<Param>, Error> {
        Ok((0..count)
            .map(|_| Param::binary(1_i32.to_be_bytes().to_vec()))
            .collect())
    };
    let err = exec::execute(&client, &mut cache, &sql, encode).await.err();
    assert!(
        matches!(
            err,
            Some(Error::Interface(_) | Error::Encode(_) | Error::Db { .. })
        ),
        "{err:?}"
    );
    assert_connection_usable(&client).await?;

    schema.drop().await
}

#[tokio::test]
async fn parameter_list_over_65535_for_one_placeholder_errors_and_keeps_connection() -> TestResult {
    let Some(schema) = common::fresh_schema().await? else {
        return Ok(());
    };
    let client = schema.connect().await?;
    let mut cache = cache();

    let encode = |_: &[Type]| -> Result<Vec<Param>, Error> {
        Ok((0..=usize::from(u16::MAX))
            .map(|_| Param::binary(1_i32.to_be_bytes().to_vec()))
            .collect())
    };
    let err = exec::execute(&client, &mut cache, "SELECT $1::int4", encode)
        .await
        .err();
    assert!(matches!(err, Some(Error::Interface(_))), "{err:?}");
    assert_connection_usable(&client).await?;

    schema.drop().await
}
