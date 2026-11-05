use std::sync::Arc;
use std::time::Duration;

use pyo3::prelude::*;
use pyo3_async_runtimes::tokio::future_into_py;
use rowx_postgres::cancel::{self, Hook};
use rowx_postgres::conn::Connection as PgConnection;
use rowx_postgres::describe::{self as pg, TypeKey};

use crate::conn::Connection;
use crate::errors::{PgError, interface_error};
use crate::exec::{fail, settle_claim, watch};
use rowx_py_core::runtime;

#[pyclass(
    frozen,
    get_all,
    skip_from_py_object,
    module = "rowx._core.postgres",
    name = "ParamInfo"
)]
#[derive(Debug, Clone)]
pub struct ParamInfo {
    oid: u32,
    type_name: String,
    type_key: Option<String>,
    element_key: Option<String>,
}

#[pyclass(
    frozen,
    get_all,
    skip_from_py_object,
    module = "rowx._core.postgres",
    name = "ColumnInfo"
)]
#[derive(Debug, Clone)]
pub struct ColumnInfo {
    name: String,
    raw_name: String,
    oid: u32,
    type_name: String,
    type_key: Option<String>,
    element_key: Option<String>,
    table_oid: Option<u32>,
    attnum: Option<i16>,
    nullable: bool,
    reason: String,
    reason_detail: Option<String>,
}

#[pyclass(
    frozen,
    get_all,
    skip_from_py_object,
    module = "rowx._core.postgres",
    name = "Describe"
)]
#[derive(Debug, Clone)]
pub struct Describe {
    params: Vec<ParamInfo>,
    columns: Vec<ColumnInfo>,
}

fn key(type_key: &TypeKey) -> Option<String> {
    type_key.as_ref().ok().map(|k| (*k).to_owned())
}

impl From<pg::ParamInfo> for ParamInfo {
    fn from(p: pg::ParamInfo) -> Self {
        Self {
            oid: p.oid,
            type_key: key(&p.type_key),
            element_key: p.element_key.map(str::to_owned),
            type_name: p.type_name,
        }
    }
}

impl From<pg::ColumnInfo> for ColumnInfo {
    fn from(c: pg::ColumnInfo) -> Self {
        Self {
            type_key: key(&c.type_key),
            element_key: c.element_key.map(str::to_owned),
            reason: c.reason.name().to_owned(),
            reason_detail: c.reason.detail().map(str::to_owned),
            name: c.name,
            raw_name: c.raw_name,
            oid: c.oid,
            type_name: c.type_name,
            table_oid: c.table_oid,
            attnum: c.attnum,
            nullable: c.nullable,
        }
    }
}

impl From<pg::Describe> for Describe {
    fn from(d: pg::Describe) -> Self {
        Self {
            params: d.params.into_iter().map(Into::into).collect(),
            columns: d.columns.into_iter().map(Into::into).collect(),
        }
    }
}

/// # Errors
/// `ConnectionBusyError`/`InterfaceError` when the connection cannot be claimed; `DatabaseError` (with
/// `.position`) when the server rejects the SQL, raised when the future is awaited.
#[pyfunction]
pub fn describe<'py>(
    py: Python<'py>,
    conn: &Connection,
    sql: String,
) -> PyResult<Bound<'py, PyAny>> {
    let cancel = conn.cancel.clone();
    let claim = cancel
        .state
        .claim()
        .map_err(|e| rowx_py_core::errors::core_to_py_err(py, e))?;
    let hook = Hook::default();
    let watched = hook.clone();
    let future = future_into_py(py, async move {
        let claim = settle_claim(&cancel.state, claim).await?;
        let inner = Arc::clone(&cancel.backend.conn);
        let pending = cancel::spawn(&cancel, claim, &hook, async move {
            let guard = inner.lock().await;
            match guard.client() {
                Ok(client) => pg::describe(client, &sql).await,
                Err(e) => Err(e),
            }
        });
        let described = pending
            .wait()
            .await
            .map_err(|e| PgError(e.into()))?
            .map_err(|e| fail(&cancel.state, e))?;
        Ok(Describe::from(described))
    })?;
    watch(&future, watched)?;
    Ok(future)
}

/// # Errors
/// `InterfaceError` for a bad `statement_timeout`; connection failures surface when the future is awaited.
#[pyfunction]
#[pyo3(signature = (dsn, statement_timeout=10.0))]
pub fn connect_checker(
    py: Python<'_>,
    dsn: String,
    statement_timeout: f64,
) -> PyResult<Bound<'_, PyAny>> {
    let timeout = Duration::try_from_secs_f64(statement_timeout).map_err(|_| {
        interface_error(
            py,
            "statement_timeout must be a non-negative number of seconds",
        )
    })?;
    let handle = runtime::handle()?;
    future_into_py(py, async move {
        let conn = PgConnection::connect_checker(&dsn, &handle, timeout)
            .await
            .map_err(PgError)?;
        Ok(Connection::new(conn, handle, 0))
    })
}
