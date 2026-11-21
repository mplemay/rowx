// pub, so the shared helpers are reachable from the crate root and need no visibility waiver
pub mod common;

use common::{TestResult, connect, fetch, file, ints, limits, memory, run};
use rowx_core::row_shape::{FieldSpec, Matching, Names, Shape, ShapeError};
use rowx_sqlite::error::Error;
use rowx_sqlite::exec::{Batch, Param, StreamLimits};
use rowx_sqlite::value::Scalar;

#[tokio::test]
async fn execute_reports_the_rows_it_changed() -> TestResult {
    let conn = connect(&memory()).await?;
    assert_eq!(run(&conn, "CREATE TABLE t (a INTEGER, b TEXT)").await?, 0);
    let inserted = conn
        .execute(
            "INSERT INTO t VALUES (?1, ?2), (?3, ?4)",
            vec![
                Param::Integer(1),
                Param::Text("one".to_owned()),
                Param::Integer(2),
                Param::Null,
            ],
        )
        .await?;
    assert_eq!(inserted, 2);
    assert_eq!(run(&conn, "UPDATE t SET b = 'x'").await?, 2);
    assert_eq!(run(&conn, "DELETE FROM t WHERE a = 1").await?, 1);
    Ok(())
}

#[tokio::test]
async fn a_statement_that_changes_nothing_reports_zero_not_the_last_count() -> TestResult {
    let conn = connect(&memory()).await?;
    run(&conn, "CREATE TABLE t (a INTEGER)").await?;
    assert_eq!(run(&conn, "INSERT INTO t VALUES (1), (2), (3)").await?, 3);
    assert_eq!(run(&conn, "CREATE TABLE other (a)").await?, 0);
    assert_eq!(run(&conn, "DELETE FROM t WHERE a = 99").await?, 0);
    assert_eq!(run(&conn, "DROP TABLE other").await?, 0);
    Ok(())
}

#[tokio::test]
async fn fetch_decodes_rows_by_declared_type() -> TestResult {
    let conn = connect(&memory()).await?;
    run(
        &conn,
        "CREATE TABLE t (a INTEGER, b TEXT, c REAL, d BOOLEAN)",
    )
    .await?;
    run(
        &conn,
        "INSERT INTO t VALUES (1, 'one', 1.5, 1), (2, NULL, NULL, 0)",
    )
    .await?;
    let fetched = fetch(&conn, "SELECT a, b, c, d FROM t ORDER BY a").await?;
    assert_eq!(fetched.columns, ["a", "b", "c", "d"]);
    assert_eq!(
        fetched.rows,
        vec![
            vec![
                Some(Scalar::Integer(1)),
                Some(Scalar::Text("one".to_owned())),
                Some(Scalar::Real(1.5)),
                Some(Scalar::Boolean(true)),
            ],
            vec![
                Some(Scalar::Integer(2)),
                None,
                None,
                Some(Scalar::Boolean(false))
            ],
        ]
    );
    assert!(fetched.mapping.is_none());
    Ok(())
}

#[tokio::test]
async fn fetch_binds_parameters() -> TestResult {
    let conn = connect(&memory()).await?;
    let fetched = conn
        .fetch(
            "SELECT ?1 + ?2",
            vec![Param::Integer(40), Param::Integer(2)],
            &Shape::default(),
        )
        .await?;
    assert_eq!(fetched.rows, vec![vec![Some(Scalar::Integer(42))]]);

    let err = conn
        .fetch("SELECT ?1", Vec::new(), &Shape::default())
        .await
        .err()
        .ok_or("a missing parameter should fail")?;
    assert!(matches!(err, Error::Interface(_)));
    Ok(())
}

#[tokio::test]
async fn limit_one_stops_after_the_first_row_and_resets_the_statement() -> TestResult {
    let conn = connect(&memory()).await?;
    let shape = Shape {
        limit_one: true,
        ..Shape::default()
    };
    let sql = "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c WHERE n < 100) SELECT n FROM c";
    for _ in 0..3 {
        let fetched = conn.fetch(sql, Vec::new(), &shape).await?;
        assert_eq!(fetched.rows, vec![vec![Some(Scalar::Integer(1))]]);
    }
    // a statement that stopped early left no cursor open
    run(&conn, "CREATE TABLE after_limit (a)").await?;
    assert_eq!(conn.rows_stepped(), 3);
    Ok(())
}

#[tokio::test]
async fn an_arity_violation_is_a_row_shape_error_before_any_row() -> TestResult {
    let conn = connect(&memory()).await?;
    let shape = Shape {
        arity: Some(2),
        ..Shape::default()
    };
    let err = conn
        .fetch("SELECT 1 WHERE 0", Vec::new(), &shape)
        .await
        .err()
        .ok_or("the arity should not fit")?;
    assert!(matches!(err, Error::RowShape(_)), "{err:?}");

    let scalar = Shape {
        scalar: true,
        ..Shape::default()
    };
    let err = conn
        .fetch("SELECT 1, 2", Vec::new(), &scalar)
        .await
        .err()
        .ok_or("a scalar is one column")?;
    assert!(matches!(err, Error::RowShape(_)), "{err:?}");
    Ok(())
}

fn field(names: &[&str]) -> FieldSpec {
    FieldSpec {
        names: Names::Known(names.iter().map(|n| (*n).to_owned()).collect()),
        required: true,
    }
}

#[tokio::test]
async fn a_named_shape_reports_mapping_errors_and_returns_the_mapping() -> TestResult {
    let conn = connect(&memory()).await?;
    let fields = [field(&["id"]), field(&["name"])];
    let mask = [false, true];
    let shape = Shape {
        mask: Some(&mask),
        fields: Some(&fields),
        matching: Matching::ByName,
        ..Shape::default()
    };

    let fetched = conn
        .fetch("SELECT 'a' AS name, 1 AS id", Vec::new(), &shape)
        .await?;
    let mapping = fetched.mapping.ok_or("a named shape maps its columns")?;
    assert_eq!(mapping.columns, vec![Some(1), Some(0)]);

    let err = conn
        .fetch("SELECT 1 AS id, 2 AS nope", Vec::new(), &shape)
        .await
        .err()
        .ok_or("an unknown column should not map")?;
    let Error::Shape { errors, columns } = err else {
        return Err("expected a mapping error".into());
    };
    assert_eq!(columns, ["id", "nope"]);
    assert!(
        errors
            .errors()
            .iter()
            .any(|e| matches!(e, ShapeError::UnexpectedColumn { index: 1, .. })),
        "{errors:?}"
    );
    Ok(())
}

#[tokio::test]
async fn a_null_in_a_non_nullable_column_is_an_error() -> TestResult {
    let conn = connect(&memory()).await?;
    let mask = [false];
    let shape = Shape {
        mask: Some(&mask),
        ..Shape::default()
    };
    let err = conn
        .fetch("SELECT NULL", Vec::new(), &shape)
        .await
        .err()
        .ok_or("NULL is not allowed")?;
    assert!(matches!(err, Error::RowShape(_)), "{err:?}");

    // a named shape names the field, the column and the row
    let fields = [field(&["a"])];
    let named = Shape {
        mask: Some(&mask),
        fields: Some(&fields),
        ..Shape::default()
    };
    let err = conn
        .fetch("SELECT 1 AS a UNION ALL SELECT NULL", Vec::new(), &named)
        .await
        .err()
        .ok_or("NULL is not allowed")?;
    assert!(
        matches!(&err, Error::NullField { column: 0, field: 0, row: 1, name } if &**name == "a"),
        "{err:?}"
    );
    Ok(())
}

#[tokio::test]
async fn a_stored_value_that_disagrees_with_its_declared_type_is_a_decode_error() -> TestResult {
    let conn = connect(&memory()).await?;
    run(&conn, "CREATE TABLE t (n INTEGER)").await?;
    // INTEGER affinity keeps text that does not look like a number
    run(&conn, "INSERT INTO t VALUES ('abc')").await?;
    let err = fetch(&conn, "SELECT n FROM t")
        .await
        .err()
        .ok_or("the stored text is not an integer")?;
    assert!(
        matches!(&err, Error::Decode(e) if e.to_string().contains("column 0")),
        "{err:?}"
    );

    // the same through a stream, which reports it at the open (the first row)
    let streamed = conn
        .stream(
            "SELECT n FROM t",
            Vec::new(),
            &Shape::default(),
            StreamLimits::default(),
        )
        .await
        .err()
        .ok_or("the stream should fail at its first row")?;
    assert!(matches!(streamed, Error::Decode(_)), "{streamed:?}");
    // and the connection is fine afterwards
    assert_eq!(ints(&conn, "SELECT 1").await?, [1]);
    Ok(())
}

#[tokio::test]
async fn a_cached_statement_is_re_prepared_after_ddl_on_another_connection() -> TestResult {
    let dir = tempfile::tempdir()?;
    let reader = connect(&file(&dir)).await?;
    let writer = connect(&file(&dir)).await?;
    run(&reader, "CREATE TABLE t (a INTEGER)").await?;
    run(&reader, "INSERT INTO t VALUES (1)").await?;

    let one = Shape {
        arity: Some(1),
        ..Shape::default()
    };
    let two = Shape {
        arity: Some(2),
        ..Shape::default()
    };
    // the second call is served from the statement cache
    for _ in 0..2 {
        let fetched = reader.fetch("SELECT * FROM t", Vec::new(), &one).await?;
        assert_eq!(fetched.rows, vec![vec![Some(Scalar::Integer(1))]]);
    }

    run(&writer, "ALTER TABLE t ADD COLUMN b INTEGER DEFAULT 7").await?;

    // the cached statement still has one column; the shape for two is judged on a fresh prepare
    let fetched = reader.fetch("SELECT * FROM t", Vec::new(), &two).await?;
    assert_eq!(fetched.columns, ["a", "b"]);
    assert_eq!(
        fetched.rows,
        vec![vec![Some(Scalar::Integer(1)), Some(Scalar::Integer(7))]]
    );
    // and the old shape no longer fits
    let err = reader
        .fetch("SELECT * FROM t", Vec::new(), &one)
        .await
        .err()
        .ok_or("the old arity should no longer fit")?;
    assert!(matches!(err, Error::RowShape(_)), "{err:?}");

    // a plan that depends on dropped schema fails cleanly, then works once the schema is back
    reader
        .fetch("SELECT a FROM t WHERE a = 1", Vec::new(), &one)
        .await?;
    run(&writer, "DROP TABLE t").await?;
    let err = fetch(&reader, "SELECT a FROM t WHERE a = 1")
        .await
        .err()
        .ok_or("the table is gone")?;
    assert!(matches!(&err, Error::Sqlite { message, .. } if message.contains("no such table")));
    run(&writer, "CREATE TABLE t (a INTEGER)").await?;
    assert_eq!(
        fetch(&reader, "SELECT a FROM t WHERE a = 1")
            .await?
            .rows
            .len(),
        0
    );
    Ok(())
}

#[tokio::test]
async fn a_stream_notices_columns_changed_since_its_statement_was_cached() -> TestResult {
    let dir = tempfile::tempdir()?;
    let reader = connect(&file(&dir)).await?;
    let writer = connect(&file(&dir)).await?;
    run(&reader, "CREATE TABLE t (a INTEGER)").await?;
    run(&reader, "INSERT INTO t VALUES (1)").await?;
    let one = Shape {
        arity: Some(1),
        ..Shape::default()
    };
    reader.fetch("SELECT * FROM t", Vec::new(), &one).await?;
    run(&writer, "ALTER TABLE t ADD COLUMN b INTEGER DEFAULT 7").await?;

    // the cached statement passes the old shape, and SQLite re-prepares it at its first step; the
    // columns are read again there, so the stale shape is caught instead of delivering wrong rows
    let err = reader
        .stream("SELECT * FROM t", Vec::new(), &one, StreamLimits::default())
        .await
        .err()
        .ok_or("the shape no longer fits")?;
    assert!(matches!(err, Error::RowShape(_)), "{err:?}");
    Ok(())
}

#[tokio::test]
async fn forget_statements_keeps_the_connection_working() -> TestResult {
    let conn = connect(&memory()).await?;
    assert_eq!(ints(&conn, "SELECT 1").await?, [1]);
    conn.forget_statements().await?;
    assert_eq!(ints(&conn, "SELECT 1").await?, [1]);
    Ok(())
}

#[tokio::test]
async fn the_statement_cache_capacity_can_be_zero() -> TestResult {
    let mut options = memory();
    options.statement_cache = 0;
    let conn = connect(&options).await?;
    for _ in 0..3 {
        assert_eq!(ints(&conn, "SELECT 1").await?, [1]);
    }
    Ok(())
}

#[tokio::test]
async fn simple_runs_one_statement_and_rejects_a_script() -> TestResult {
    let conn = connect(&memory()).await?;
    conn.simple("CREATE TABLE t (a)").await?;
    conn.simple("INSERT INTO t VALUES (1)").await?;
    let err = conn
        .simple("INSERT INTO t VALUES (2); INSERT INTO t VALUES (3)")
        .await
        .err()
        .ok_or("a script is not one statement")?;
    assert!(matches!(err, Error::Interface(_)), "{err:?}");
    assert_eq!(ints(&conn, "SELECT count(*) FROM t").await?, [1]);
    Ok(())
}

#[tokio::test]
async fn the_migration_script_runs_every_statement_and_stops_at_the_first_failure() -> TestResult {
    let conn = connect(&memory()).await?;
    conn.execute_migration_script(
        "CREATE TABLE t (a INTEGER); INSERT INTO t VALUES (1); INSERT INTO t VALUES (2);",
    )
    .await?;
    assert_eq!(ints(&conn, "SELECT a FROM t ORDER BY a").await?, [1, 2]);

    let err = conn
        .execute_migration_script("INSERT INTO t VALUES (3); INSERT INTO missing VALUES (4)")
        .await
        .err()
        .ok_or("the second statement should fail")?;
    assert!(matches!(&err, Error::Sqlite { message, .. } if message.contains("no such table")));
    assert_eq!(ints(&conn, "SELECT a FROM t ORDER BY a").await?, [1, 2, 3]);
    Ok(())
}

#[tokio::test]
async fn autocommit_ping_and_reset_follow_the_transaction() -> TestResult {
    let conn = connect(&memory()).await?;
    conn.ping().await?;
    assert!(conn.is_autocommit().await?);
    conn.simple("BEGIN").await?;
    assert!(!conn.is_autocommit().await?);
    conn.reset(false).await?;
    assert!(!conn.is_autocommit().await?);
    conn.reset(true).await?;
    assert!(conn.is_autocommit().await?);
    conn.simple("SAVEPOINT s").await?;
    conn.simple("RELEASE s").await?;
    assert!(conn.is_autocommit().await?);
    Ok(())
}

#[tokio::test]
async fn a_stream_delivers_batches_then_the_end_with_rows_affected() -> TestResult {
    let conn = connect(&memory()).await?;
    run(&conn, "CREATE TABLE t (a INTEGER)").await?;
    let sql = "INSERT INTO t SELECT value FROM (WITH RECURSIVE c(value) AS (SELECT 1 UNION ALL SELECT value + 1 FROM c WHERE value < 7) SELECT value FROM c) RETURNING a";
    let mut streaming = conn
        .stream(sql, Vec::new(), &Shape::default(), limits(3, 2))
        .await?;
    let mut sizes = Vec::new();
    let end = loop {
        match streaming.batches.next_batch().await? {
            Batch::Rows(rows) => sizes.push(rows.len()),
            Batch::End { rows_affected } => break rows_affected,
        }
    };
    assert_eq!(sizes, [3, 3, 1]);
    assert_eq!(end, Some(7));
    // the end repeats
    assert!(matches!(
        streaming.batches.next_batch().await?,
        Batch::End {
            rows_affected: Some(7)
        }
    ));
    streaming.done.wait().await;
    assert_eq!(ints(&conn, "SELECT count(*) FROM t").await?, [7]);

    let mut select = conn
        .stream(
            "SELECT a FROM t",
            Vec::new(),
            &Shape::default(),
            limits(10, 1),
        )
        .await?;
    assert_eq!(select.batches.columns(), ["a"]);
    assert!(matches!(
        select.batches.next_batch().await?,
        Batch::Rows(rows) if rows.len() == 7
    ));
    assert!(matches!(
        select.batches.next_batch().await?,
        Batch::End {
            rows_affected: None
        }
    ));
    Ok(())
}

#[tokio::test]
async fn a_stream_with_no_rows_still_checks_its_shape_and_ends() -> TestResult {
    let conn = connect(&memory()).await?;
    let mut streaming = conn
        .stream(
            "SELECT 1 WHERE 0",
            Vec::new(),
            &Shape::default(),
            StreamLimits::default(),
        )
        .await?;
    assert!(matches!(
        streaming.batches.next_batch().await?,
        Batch::End { .. }
    ));
    let shape = Shape {
        arity: Some(3),
        ..Shape::default()
    };
    let err = conn
        .stream(
            "SELECT 1 WHERE 0",
            Vec::new(),
            &shape,
            StreamLimits::default(),
        )
        .await
        .err()
        .ok_or("the arity should not fit")?;
    assert!(matches!(err, Error::RowShape(_)));
    Ok(())
}

#[tokio::test]
async fn a_failure_after_some_rows_arrives_after_those_rows() -> TestResult {
    let conn = connect(&memory()).await?;
    run(&conn, "CREATE TABLE t (n INTEGER)").await?;
    run(&conn, "INSERT INTO t VALUES (1), (2), (3), ('bad'), (5)").await?;
    let mut streaming = conn
        .stream(
            "SELECT n FROM t",
            Vec::new(),
            &Shape::default(),
            limits(10, 2),
        )
        .await?;
    match streaming.batches.next_batch().await? {
        Batch::Rows(rows) => assert_eq!(rows.len(), 3),
        Batch::End { .. } => return Err("the rows come first".into()),
    }
    let err = streaming
        .batches
        .next_batch()
        .await
        .err()
        .ok_or("the bad row should fail the stream")?;
    assert!(matches!(err, Error::Decode(_)), "{err:?}");
    assert!(matches!(
        streaming.batches.next_batch().await?,
        Batch::End {
            rows_affected: None
        }
    ));
    streaming.done.wait().await;
    Ok(())
}

#[tokio::test]
async fn execute_of_a_row_returning_write_is_refused_before_it_runs() -> TestResult {
    let conn = connect(&memory()).await?;
    run(&conn, "CREATE TABLE t (a INTEGER)").await?;
    let err = run(&conn, "INSERT INTO t VALUES (1) RETURNING a")
        .await
        .err()
        .ok_or("a statement that returns rows is not an execute")?;
    assert!(matches!(err, Error::Interface(_)), "{err:?}");
    // the write was never applied
    assert_eq!(ints(&conn, "SELECT count(*) FROM t").await?, [0]);
    Ok(())
}

#[tokio::test]
async fn execute_of_a_select_is_refused_without_effect() -> TestResult {
    let conn = connect(&memory()).await?;
    let err = run(&conn, "SELECT 1")
        .await
        .err()
        .ok_or("a query is not an execute")?;
    assert!(matches!(err, Error::Interface(_)), "{err:?}");
    assert_eq!(ints(&conn, "SELECT 2").await?, [2]);
    Ok(())
}
