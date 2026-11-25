pub mod common;

use common::{TestResult, backend_of, connect, failure, file_options, memory, path, pragma, run};
use rowx_core::cancel::Backend;
use rowx_sqlite::conn::ConnectOptions;
use rowx_sqlite::conn::Connection;
use rowx_sqlite::describe::{ColumnInfo, Construct, Describe, NullReason, Structure};
use rowx_sqlite::dsn::{OpenMode, Target};
use rowx_sqlite::error::Error;
use rowx_sqlite::types::Kind;

const SCHEMA: &str = "
    CREATE TABLE t (id INTEGER PRIMARY KEY, a INTEGER NOT NULL, b TEXT, c, g INTEGER GENERATED ALWAYS AS (a + 1) VIRTUAL);
    CREATE TABLE s (id INTEGER PRIMARY KEY, x TEXT NOT NULL) STRICT;
    CREATE TABLE r (a TEXT NOT NULL, b TEXT);
    CREATE TABLE w (k INTEGER PRIMARY KEY, v TEXT) WITHOUT ROWID;
    CREATE TABLE p (k INTEGER PRIMARY KEY DESC, v TEXT);
    CREATE TABLE z (a TEXT, b TEXT, PRIMARY KEY (a, b));
    CREATE VIEW v AS SELECT a, b FROM t;
    CREATE VIEW vl AS SELECT t.a, s.x FROM t LEFT JOIN s ON 1;
";

async fn schema() -> TestResult<Connection> {
    let conn = connect(&memory()).await?;
    conn.run_raw(|raw| raw.execute_batch(SCHEMA)).await??;
    Ok(conn)
}

fn names(parts: &[&str]) -> Vec<String> {
    parts.iter().map(|part| (*part).to_owned()).collect()
}

fn from(tables: &[&str]) -> Structure {
    Structure {
        sources: tables.iter().map(|table| names(&[table])).collect(),
        ..Structure::default()
    }
}

async fn describe(conn: &Connection, sql: &str, structure: Structure) -> Result<Describe, Error> {
    conn.describe(sql, structure).await
}

fn column<'d>(described: &'d Describe, name: &str) -> TestResult<&'d ColumnInfo> {
    described
        .columns
        .iter()
        .find(|col| col.name == name)
        .ok_or_else(|| format!("no column {name}").into())
}

// (decltype, origin database, table, column, not null, primary key) of each column of `sql`
type Spike = Vec<(Option<String>, Option<(String, String, String)>, bool, bool)>;

async fn spike(conn: &Connection, sql: &'static str) -> TestResult<Spike> {
    let out = conn
        .run_raw(move |raw| -> Result<Spike, rusqlite::Error> {
            let stmt = raw.prepare(sql)?;
            let mut out = Vec::new();
            for (index, column) in stmt.columns().iter().enumerate() {
                let declared = column.decl_type().map(str::to_owned);
                match stmt.column_metadata(index)? {
                    Some((db, table, origin, _, _, not_null, pk, _)) => out.push((
                        declared,
                        Some((
                            db.to_string_lossy().into_owned(),
                            table.to_string_lossy().into_owned(),
                            origin.to_string_lossy().into_owned(),
                        )),
                        not_null,
                        pk,
                    )),
                    None => out.push((declared, None, false, false)),
                }
            }
            Ok(out)
        })
        .await??;
    Ok(out)
}

fn base(table: &str, column: &str) -> (String, String, String) {
    ("main".to_owned(), table.to_owned(), column.to_owned())
}

// the spike: what the pinned rusqlite 0.39.0 (SQLite 3.51.3) reports for each construct

#[tokio::test]
async fn spike_a_base_column_reports_its_type_origin_and_flags() -> TestResult {
    let conn = schema().await?;
    let got = spike(&conn, "SELECT a, b, c, id FROM t").await?;
    assert_eq!(
        got,
        vec![
            (
                Some("INTEGER".to_owned()),
                Some(base("t", "a")),
                true,
                false
            ),
            (Some("TEXT".to_owned()), Some(base("t", "b")), false, false),
            (None, Some(base("t", "c")), false, false),
            // the rowid alias is a primary key, and its NOT NULL flag is clear
            (
                Some("INTEGER".to_owned()),
                Some(base("t", "id")),
                false,
                true
            ),
        ]
    );
    Ok(())
}

#[tokio::test]
async fn spike_an_expression_has_no_type_and_no_origin() -> TestResult {
    let conn = schema().await?;
    for sql in [
        "SELECT a + 1 FROM t",
        "SELECT 1",
        "SELECT count(*) FROM t",
        "SELECT abs(a) FROM t",
        "SELECT CAST(a AS TEXT) FROM t",
    ] {
        assert_eq!(
            spike(&conn, sql).await?,
            vec![(None, None, false, false)],
            "{sql}"
        );
    }
    Ok(())
}

#[tokio::test]
async fn spike_origin_resolves_through_views_ctes_and_subqueries() -> TestResult {
    let conn = schema().await?;
    let direct = vec![(
        Some("INTEGER".to_owned()),
        Some(base("t", "a")),
        true,
        false,
    )];
    for sql in [
        "SELECT a FROM v",
        "WITH c AS (SELECT a FROM t) SELECT a FROM c",
        "SELECT a FROM (SELECT a FROM t)",
    ] {
        assert_eq!(spike(&conn, sql).await?, direct, "{sql}");
    }
    // an outer join keeps the origin and the NOT NULL flag, which the join makes wrong: this is why
    // the flag is only trusted without one
    assert_eq!(
        spike(&conn, "SELECT x FROM vl").await?,
        vec![(Some("TEXT".to_owned()), Some(base("s", "x")), true, false)]
    );
    Ok(())
}

#[tokio::test]
async fn spike_a_scalar_subquery_column_takes_the_origin_of_its_select() -> TestResult {
    let conn = schema().await?;
    assert_eq!(
        spike(&conn, "SELECT (SELECT x FROM s)").await?,
        vec![(Some("TEXT".to_owned()), Some(base("s", "x")), true, false)]
    );
    Ok(())
}

#[tokio::test]
async fn spike_a_compound_select_takes_its_metadata_from_the_leftmost_arm() -> TestResult {
    let conn = schema().await?;
    assert_eq!(
        spike(&conn, "SELECT a FROM t UNION ALL SELECT NULL").await?,
        vec![(
            Some("INTEGER".to_owned()),
            Some(base("t", "a")),
            true,
            false
        )]
    );
    assert_eq!(
        spike(&conn, "SELECT NULL UNION ALL SELECT a FROM t").await?,
        vec![(None, None, false, false)]
    );
    Ok(())
}

#[tokio::test]
async fn spike_a_rowid_alias_is_not_flagged_and_other_keys_are_flagged_as_primary() -> TestResult {
    let conn = schema().await?;
    // WITHOUT ROWID makes SQLite flag the key NOT NULL; a DESC key is not a rowid alias and is not flagged
    let flags = |got: Spike| {
        got.into_iter()
            .map(|(_, _, nn, pk)| (nn, pk))
            .collect::<Vec<_>>()
    };
    assert_eq!(
        flags(spike(&conn, "SELECT k FROM w").await?),
        [(true, true)]
    );
    assert_eq!(
        flags(spike(&conn, "SELECT k FROM p").await?),
        [(false, true)]
    );
    assert_eq!(
        flags(spike(&conn, "SELECT a, b FROM z").await?),
        [(false, true), (false, true)]
    );
    // a generated column is not flagged even when its inputs are NOT NULL
    assert_eq!(
        flags(spike(&conn, "SELECT g FROM t").await?),
        [(false, false)]
    );
    Ok(())
}

#[tokio::test]
async fn a_provable_statement_trusts_not_null_and_the_rowid_alias() -> TestResult {
    let conn = schema().await?;
    let got = describe(&conn, "SELECT id, a, b, c FROM t", from(&["t"])).await?;
    let reasons: Vec<_> = got
        .columns
        .iter()
        .map(|col| (col.name.as_str(), col.nullable, col.reason.name()))
        .collect();
    assert_eq!(
        reasons,
        [
            ("id", false, "NotNullColumn"),
            ("a", false, "NotNullColumn"),
            ("b", true, "NullableColumn"),
            ("c", true, "NullableColumn"),
        ]
    );
    let c = column(&got, "c")?;
    assert_eq!((c.kind, c.type_name.as_str()), (None, ""));
    let a = column(&got, "a")?;
    assert_eq!(a.kind, Some(Kind::Integer));
    assert_eq!(a.origin.as_ref().map(|o| o.column.as_str()), Some("a"));
    Ok(())
}

#[tokio::test]
async fn an_expression_is_unproven() -> TestResult {
    let conn = schema().await?;
    let got = describe(&conn, "SELECT b || 'x' AS n FROM t", from(&["t"])).await?;
    let n = column(&got, "n")?;
    assert!(n.nullable);
    assert_eq!(
        n.reason,
        NullReason::Unproven {
            cause: "expression".to_owned()
        }
    );
    Ok(())
}

#[tokio::test]
async fn a_view_source_makes_every_column_unproven() -> TestResult {
    let conn = schema().await?;
    let got = describe(&conn, "SELECT b FROM v", from(&["v"])).await?;
    let b = column(&got, "b")?;
    assert!(b.nullable);
    assert_eq!(b.reason.name(), "Unproven");
    assert_eq!(b.reason.detail(), Some("view v"));
    Ok(())
}

#[tokio::test]
async fn views_resolve_case_insensitively_and_in_a_schema() -> TestResult {
    let conn = schema().await?;
    for (sql, parts) in [
        ("SELECT b FROM V", &["v"][..]),
        ("SELECT b FROM main.v", &["main", "v"]),
    ] {
        let structure = Structure {
            sources: vec![names(parts)],
            ..Structure::default()
        };
        let got = describe(&conn, sql, structure).await?;
        assert_eq!(
            column(&got, "b")?.reason.detail(),
            Some(&*format!("view {}", parts.join("."))),
            "{sql}"
        );
    }
    Ok(())
}

#[tokio::test]
async fn a_temp_table_shadows_a_main_table_of_the_same_name() -> TestResult {
    let conn = schema().await?;
    // the temp view named like a main table is what the name means, and it is not an ordinary table; its
    // column is NULL, so the bytecode proves nothing for it either
    conn.run_raw(|raw| raw.execute_batch("CREATE TEMP VIEW s AS SELECT NULL AS x"))
        .await??;
    let got = describe(&conn, "SELECT x FROM s", from(&["s"])).await?;
    assert_eq!(column(&got, "x")?.reason.detail(), Some("view s"));
    let got = describe(
        &conn,
        "SELECT x FROM main.s",
        Structure {
            sources: vec![names(&["main", "s"])],
            ..Structure::default()
        },
    )
    .await?;
    assert_eq!(column(&got, "x")?.reason.name(), "NotNullColumn");
    Ok(())
}

#[tokio::test]
async fn virtual_tables_table_functions_and_unknown_names_are_not_tables() -> TestResult {
    let conn = schema().await?;
    conn.run_raw(|raw| raw.execute_batch("CREATE VIRTUAL TABLE vt USING rtree(i, lo, hi)"))
        .await??;
    for (sql, name) in [
        ("SELECT value FROM json_each('[1]')", "json_each"),
        ("SELECT name FROM sqlite_master", "sqlite_master"),
        ("SELECT lo FROM vt", "vt"),
    ] {
        let got = describe(&conn, sql, from(&[name])).await?;
        let detail = format!("source {name} is not a table");
        assert_eq!(
            got.columns[0].reason.detail(),
            Some(detail.as_str()),
            "{sql}"
        );
    }
    Ok(())
}

#[tokio::test]
async fn each_structural_flag_makes_a_base_column_unproven() -> TestResult {
    let conn = schema().await?;
    let flags = [
        (
            "common table expression",
            Structure {
                constructs: vec![Construct::CommonTableExpression],
                ..from(&["t"])
            },
        ),
        (
            "subquery",
            Structure {
                constructs: vec![Construct::Subquery],
                ..from(&["t"])
            },
        ),
        (
            "compound select",
            Structure {
                constructs: vec![Construct::CompoundSelect],
                ..from(&["t"])
            },
        ),
        (
            "outer join",
            Structure {
                constructs: vec![Construct::OuterJoin],
                ..from(&["t"])
            },
        ),
    ];
    for (cause, structure) in flags {
        // a nullable column, so the bytecode proves nothing the structure denies
        let got = describe(&conn, "SELECT b FROM t", structure).await?;
        let b = column(&got, "b")?;
        assert!(b.nullable, "{cause}");
        assert_eq!(b.reason.detail(), Some(cause));
    }
    Ok(())
}

#[tokio::test]
async fn a_rowid_alias_is_non_null_only_for_a_real_alias() -> TestResult {
    let conn = schema().await?;
    for (sql, table, column_name, nullable) in [
        ("SELECT id FROM t", "t", "id", false),
        ("SELECT rowid FROM t", "t", "id", false),
        ("SELECT id FROM s", "s", "id", false),
        // a DESC key is not an alias, and a composite key never is; neither is flagged NOT NULL
        ("SELECT k FROM p", "p", "k", true),
        ("SELECT a FROM z", "z", "a", true),
        // WITHOUT ROWID makes SQLite flag the key itself
        ("SELECT k FROM w", "w", "k", false),
    ] {
        let got = describe(&conn, sql, from(&[table])).await?;
        assert_eq!(column(&got, column_name)?.nullable, nullable, "{sql}");
    }
    Ok(())
}

#[tokio::test]
async fn the_implicit_rowid_of_a_table_without_an_alias_is_non_null() -> TestResult {
    let conn = schema().await?;
    let got = describe(&conn, "SELECT rowid, oid, _rowid_ FROM r", from(&["r"])).await?;
    assert!(got.columns.iter().all(|col| !col.nullable), "{got:?}");
    Ok(())
}

#[tokio::test]
async fn the_overrides_apply_whether_or_not_the_statement_is_provable() -> TestResult {
    let conn = schema().await?;
    for structure in [
        from(&["t"]),
        Structure {
            constructs: vec![Construct::OuterJoin],
            ..from(&["t"])
        },
    ] {
        let got = describe(
            &conn,
            "SELECT a AS \"a?\", b AS \"b!\", 1 AS \"n!!\" FROM t",
            structure,
        )
        .await?;
        let [question, bang, twice] = got.columns.as_slice() else {
            return Err("expected three columns".into());
        };
        assert_eq!(
            (question.name.as_str(), question.raw_name.as_str()),
            ("a", "a?")
        );
        assert_eq!(
            (question.nullable, question.reason.name()),
            (true, "OverrideQuestion")
        );
        assert_eq!(
            (bang.name.as_str(), bang.nullable, bang.reason.name()),
            ("b", false, "OverrideBang")
        );
        assert_eq!(
            (twice.name.as_str(), twice.raw_name.as_str(), twice.nullable),
            ("n!", "n!!", false)
        );
    }
    Ok(())
}

#[tokio::test]
async fn parameters_report_their_count_and_names() -> TestResult {
    let conn = schema().await?;
    let got = describe(
        &conn,
        "SELECT a FROM t WHERE a = ?1 AND b = :n AND c = ?",
        from(&["t"]),
    )
    .await?;
    let names: Vec<_> = got.params.iter().map(|p| p.name.as_deref()).collect();
    assert_eq!(names, [Some("?1"), Some(":n"), None]);
    let none = describe(&conn, "SELECT a FROM t", from(&["t"])).await?;
    assert_eq!(none.params, vec![]);
    Ok(())
}

#[tokio::test]
async fn a_statement_without_columns_describes_as_empty() -> TestResult {
    let conn = schema().await?;
    let got = describe(&conn, "DELETE FROM t WHERE a = ?", from(&["t"])).await?;
    assert_eq!(got.columns, vec![]);
    assert_eq!(got.params.len(), 1);
    Ok(())
}

#[tokio::test]
async fn describing_never_runs_the_statement() -> TestResult {
    let conn = schema().await?;
    describe(
        &conn,
        "INSERT INTO t (a) VALUES (1) RETURNING a",
        from(&["t"]),
    )
    .await?;
    describe(&conn, "DELETE FROM t", from(&["t"])).await?;
    let count = conn
        .run_raw(|raw| raw.query_row("SELECT count(*) FROM t", [], |row| row.get::<_, i64>(0)))
        .await??;
    assert_eq!(count, 0);
    assert_eq!(run(&conn, "INSERT INTO t (a) VALUES (7)").await?, 1);
    Ok(())
}

#[tokio::test]
async fn rejected_sql_carries_the_error_offset() -> TestResult {
    let conn = schema().await?;
    let err = describe(&conn, "SELECT FROM WHERE", Structure::default())
        .await
        .err();
    assert!(
        matches!(
            &err,
            Some(Error::Sqlite {
                offset: Some(_),
                ..
            })
        ),
        "{err:?}"
    );
    let err = describe(&conn, "SELECT nope FROM t", from(&["t"]))
        .await
        .err();
    assert!(
        matches!(
            &err,
            Some(Error::Sqlite {
                offset: Some(_),
                ..
            })
        ),
        "{err:?}"
    );
    Ok(())
}

#[tokio::test]
async fn empty_sql_and_a_second_statement_are_rejected() -> TestResult {
    let conn = schema().await?;
    for sql in ["", "   ", "-- nothing", "/* nothing */ ;"] {
        let err = describe(&conn, sql, Structure::default()).await.err();
        assert!(
            matches!(&err, Some(Error::NotOneStatement(text)) if text.contains("no statement")),
            "{sql:?}: {err:?}"
        );
    }
    let err = describe(&conn, "SELECT 1; SELECT 2", Structure::default())
        .await
        .err();
    assert!(
        matches!(&err, Some(Error::NotOneStatement(text)) if text.contains("exactly one")),
        "{err:?}"
    );
    // a trailing semicolon and a trailing comment are not a second statement
    for sql in ["SELECT 1;", "SELECT 1; -- done", "SELECT 1 /* done */"] {
        describe(&conn, sql, Structure::default()).await?;
    }
    Ok(())
}

#[tokio::test]
async fn the_checker_session_refuses_writes_and_distrusts_the_schema() -> TestResult {
    let dir = tempfile::tempdir()?;
    let writer = connect(&file_options(&dir, "check.db", OpenMode::ReadWriteCreate)).await?;
    run(&writer, "CREATE TABLE t (a INTEGER NOT NULL)").await?;
    let mut options = file_options(&dir, "check.db", OpenMode::ReadOnly);
    options.read_only = true;
    options.untrusted = true;
    let checker = connect(&options).await?;
    let err = run(&checker, "INSERT INTO t VALUES (1)").await.err();
    assert!(
        matches!(&err, Some(Error::Sqlite { name, .. }) if name.starts_with("SQLITE_READONLY")),
        "{err:?}"
    );
    let got = describe(&checker, "SELECT a FROM t", from(&["t"])).await?;
    assert_eq!(got.columns[0].reason.name(), "NotNullColumn");
    Ok(())
}

#[tokio::test]
async fn a_view_using_an_untrusted_function_fails_to_describe_when_the_schema_is_distrusted()
-> TestResult {
    let dir = tempfile::tempdir()?;
    let writer = connect(&file_options(&dir, "check.db", OpenMode::ReadWriteCreate)).await?;
    run(&writer, "CREATE TABLE t (a INTEGER)").await?;
    run(
        &writer,
        "CREATE VIEW u AS SELECT a, rtreedepth(zeroblob(8)) AS r FROM t",
    )
    .await?;
    let sql = "SELECT a FROM u";
    describe(&writer, sql, from(&["u"])).await?;
    let mut options = file_options(&dir, "check.db", OpenMode::ReadOnly);
    options.read_only = true;
    options.untrusted = true;
    let checker = connect(&options).await?;
    let err = describe(&checker, sql, from(&["u"])).await.err();
    assert!(
        matches!(&err, Some(Error::Sqlite { message, .. }) if message.contains("unsafe use of rtreedepth")),
        "{err:?}"
    );
    Ok(())
}

#[tokio::test]
async fn a_describe_sees_a_schema_another_connection_changed() -> TestResult {
    let dir = tempfile::tempdir()?;
    let writer = connect(&file_options(&dir, "check.db", OpenMode::ReadWriteCreate)).await?;
    run(&writer, "CREATE TABLE t (a INTEGER)").await?;
    let checker = connect(&ConnectOptions::checker(
        Target::File(path(&dir, "check.db")),
        None,
    ))
    .await?;
    let before = describe(&checker, "SELECT * FROM t", from(&["t"])).await?;
    assert_eq!(before.columns.len(), 1);
    run(&writer, "ALTER TABLE t ADD COLUMN b TEXT").await?;
    let after = describe(&checker, "SELECT * FROM t", from(&["t"])).await?;
    let names: Vec<&str> = after.columns.iter().map(|col| col.name.as_str()).collect();
    assert_eq!(names, ["a", "b"]);
    Ok(())
}

#[tokio::test]
async fn describing_a_pragma_that_sets_a_value_is_refused_and_changes_nothing() -> TestResult {
    let dir = tempfile::tempdir()?;
    let writer = connect(&file_options(&dir, "check.db", OpenMode::ReadWriteCreate)).await?;
    run(&writer, "CREATE TABLE t (a INTEGER)").await?;
    run(
        &writer,
        "CREATE VIEW u AS SELECT a, rtreedepth(zeroblob(8)) AS r FROM t",
    )
    .await?;
    let checker = connect(&ConnectOptions::checker(
        Target::File(path(&dir, "check.db")),
        None,
    ))
    .await?;
    for sql in [
        "PRAGMA trusted_schema=ON",
        "PRAGMA query_only=OFF",
        "PRAGMA trusted_schema(1)",
    ] {
        let err = describe(&checker, sql, Structure::default()).await.err();
        assert!(
            matches!(&err, Some(Error::Sqlite { name, .. }) if *name == "SQLITE_AUTH"),
            "{sql}: {err:?}"
        );
    }
    assert_eq!(pragma(&checker, "trusted_schema").await?, 0);
    assert_eq!(pragma(&checker, "query_only").await?, 1);
    let err = describe(&checker, "SELECT a FROM u", from(&["u"]))
        .await
        .err();
    assert!(
        matches!(&err, Some(Error::Sqlite { message, .. }) if message.contains("unsafe use of rtreedepth")),
        "{err:?}"
    );
    Ok(())
}

#[tokio::test]
async fn read_only_pragmas_still_describe_on_the_checker() -> TestResult {
    let dir = tempfile::tempdir()?;
    let writer = connect(&file_options(&dir, "check.db", OpenMode::ReadWriteCreate)).await?;
    run(&writer, "CREATE TABLE t (a INTEGER)").await?;
    let checker = connect(&ConnectOptions::checker(
        Target::File(path(&dir, "check.db")),
        None,
    ))
    .await?;
    for sql in ["PRAGMA table_info(t)", "PRAGMA trusted_schema"] {
        describe(&checker, sql, Structure::default()).await?;
    }
    Ok(())
}

#[tokio::test]
async fn a_runtime_connection_still_sets_pragmas() -> TestResult {
    let conn = connect(&memory()).await?;
    run(&conn, "PRAGMA recursive_triggers = ON").await?;
    assert_eq!(pragma(&conn, "recursive_triggers").await?, 1);
    Ok(())
}

#[tokio::test]
async fn a_describe_after_a_cancel_fails_as_an_interrupt() -> TestResult {
    let conn = schema().await?;
    backend_of(&conn).send_cancel().await?;
    let err = describe(&conn, "SELECT a + 1 FROM t", from(&["t"]))
        .await
        .err()
        .ok_or("the flag should stop the describe")?;
    assert_eq!(failure(&err), Some(("SQLITE_INTERRUPT", "57014")));
    Ok(())
}
