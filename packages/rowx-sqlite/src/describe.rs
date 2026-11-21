use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3_async_runtimes::tokio::future_into_py;
use rowx_py_core::runtime;
use rowx_py_core::time::seconds;
use rowx_sqlite::conn::{ConnectOptions, Connection as SqliteConnection};
use rowx_sqlite::describe::{self as sqlite, Construct, Structure};
use rowx_sqlite::dsn;
use rowx_sqlite::error::Error;
use std::time::Duration;

use crate::conn::Connection;
use crate::errors::{Failure, SqliteError, to_py_err};
use crate::exec;
use crate::types::key_of;

#[pyclass(
    frozen,
    get_all,
    skip_from_py_object,
    module = "rowx_sqlite._core",
    name = "ParamInfo"
)]
#[derive(Debug, Clone)]
pub struct ParamInfo {
    name: Option<String>,
    type_name: String,
    type_key: Option<String>,
    element_key: Option<String>,
}

#[pyclass(
    frozen,
    get_all,
    skip_from_py_object,
    module = "rowx_sqlite._core",
    name = "ColumnInfo"
)]
#[derive(Debug, Clone)]
pub struct ColumnInfo {
    name: String,
    raw_name: String,
    type_name: String,
    type_key: Option<String>,
    element_key: Option<String>,
    nullable: bool,
    reason: String,
    reason_detail: Option<String>,
    database: Option<String>,
    table: Option<String>,
    column: Option<String>,
}

#[pyclass(
    frozen,
    get_all,
    skip_from_py_object,
    module = "rowx_sqlite._core",
    name = "Describe"
)]
#[derive(Debug, Clone)]
pub struct Describe {
    params: Vec<ParamInfo>,
    columns: Vec<ColumnInfo>,
}

impl From<sqlite::ParamInfo> for ParamInfo {
    fn from(param: sqlite::ParamInfo) -> Self {
        // a parameter has no type of its own in SQLite, so it is untyped
        Self {
            name: param.name,
            type_name: String::new(),
            type_key: None,
            element_key: None,
        }
    }
}

impl From<sqlite::ColumnInfo> for ColumnInfo {
    fn from(col: sqlite::ColumnInfo) -> Self {
        let (database, table, column) = match col.origin {
            Some(origin) => (
                Some(origin.database),
                Some(origin.table),
                Some(origin.column),
            ),
            None => (None, None, None),
        };
        Self {
            type_key: col.kind.and_then(key_of).map(str::to_owned),
            element_key: None,
            reason: col.reason.name().to_owned(),
            reason_detail: col.reason.detail().map(str::to_owned),
            name: col.name,
            raw_name: col.raw_name,
            type_name: col.type_name,
            nullable: col.nullable,
            database,
            table,
            column,
        }
    }
}

impl From<sqlite::Describe> for Describe {
    fn from(described: sqlite::Describe) -> Self {
        Self {
            params: described.params.into_iter().map(Into::into).collect(),
            columns: described.columns.into_iter().map(Into::into).collect(),
        }
    }
}

fn construct(name: &str) -> PyResult<Construct> {
    match name {
        "cte" => Ok(Construct::CommonTableExpression),
        "subquery" => Ok(Construct::Subquery),
        "compound" => Ok(Construct::CompoundSelect),
        "outer_join" => Ok(Construct::OuterJoin),
        "aggregate" => Ok(Construct::Aggregate),
        other => Err(PyValueError::new_err(format!(
            "unknown construct {other:?}"
        ))),
    }
}

/// `constructs` names what the lexer found in the statement: `cte`, `subquery`, `compound`,
/// `outer_join` or `aggregate`.
///
/// # Errors
/// `ValueError` for an unknown construct name; `ConnectionBusyError`/`InterfaceError` when the connection
/// cannot be claimed; `DatabaseError` (with `.position`) when SQLite rejects the SQL, `InterfaceError` for
/// no statement or more than one, raised when the future is awaited.
#[pyfunction]
#[pyo3(signature = (conn, sql, sources, *, constructs))]
pub fn describe<'py>(
    py: Python<'py>,
    conn: &Connection,
    sql: String,
    sources: Vec<Vec<String>>,
    constructs: Vec<String>,
) -> PyResult<Bound<'py, PyAny>> {
    let constructs = constructs
        .into_iter()
        .map(|name| construct(&name))
        .collect::<PyResult<Vec<_>>>()?;
    let structure = Structure {
        sources,
        constructs,
    };
    exec::run(py, conn.core(), move |core, _| async move {
        let guard = core.conn.lock().await;
        guard
            .describe(&sql, structure)
            .await
            .map(Describe::from)
            .map_err(|err| Failure::at(err, &sql))
    })
}

/// Opens the session the checker describes through: read-only, `query_only`, `trusted_schema` off and
/// the defensive flag on.
///
/// # Errors
/// `InterfaceError` for a bad DSN, `ValueError` for a bad `statement_timeout`; open failures surface when
/// the future is awaited.
#[pyfunction]
#[pyo3(signature = (dsn, statement_timeout=10.0))]
pub fn connect_checker<'py>(
    py: Python<'py>,
    dsn: &str,
    statement_timeout: f64,
) -> PyResult<Bound<'py, PyAny>> {
    let target = dsn::parse(dsn).map_err(|e| to_py_err(py, &Failure::from(Error::Dsn(e))))?;
    let timeout = seconds("statement_timeout", statement_timeout)?;
    // like `connect`, zero means no limit
    let options = ConnectOptions::checker(
        target,
        Some(timeout).filter(|limit| *limit > Duration::ZERO),
    );
    let handle = runtime::handle()?;
    future_into_py(py, async move {
        let conn = SqliteConnection::connect(&options)
            .await
            .map_err(SqliteError::from)?;
        Ok(Connection::new(conn, handle, options.statement_cache, true))
    })
}
