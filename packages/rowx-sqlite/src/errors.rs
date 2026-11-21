use pyo3::prelude::*;
use rowx_core::error::{CoreError, TaskStopped};
use rowx_py_core::errors as shared;
use rowx_sqlite::error::Error;

use crate::named::RowShape;

// the text PostgreSQL gives for SQLSTATE 25P02
const ABORTED_MESSAGE: &str =
    "current transaction is aborted, commands ignored until end of transaction block";
const ABORTED_SQLSTATE: &str = "25P02";
// SQLite ends the transaction itself when an interrupt (or a full disk) stops a write
const ABORTED_CODE: &str = "SQLITE_INTERRUPT";

/// What a call can fail with: a driver error, or a statement refused because SQLite already ended the
/// open `transaction()` (an interrupted write rolls the whole transaction back).
#[derive(Debug)]
pub enum Failure {
    Driver {
        error: Error,
        // the 1-based character index of the error in the statement, when SQLite pointed at one
        position: Option<u32>,
    },
    Aborted,
}

impl Failure {
    /// A driver error raised by `sql`; SQLite's byte offset becomes a 1-based character index.
    #[must_use]
    pub fn at(error: Error, sql: &str) -> Self {
        let position = match &error {
            Error::Sqlite {
                offset: Some(offset),
                ..
            } => char_position(sql, *offset),
            _ => None,
        };
        Self::Driver { error, position }
    }
}

impl From<Error> for Failure {
    fn from(error: Error) -> Self {
        Self::Driver {
            error,
            position: None,
        }
    }
}

impl From<CoreError> for Failure {
    fn from(error: CoreError) -> Self {
        Error::Core(error).into()
    }
}

impl From<TaskStopped> for Failure {
    fn from(error: TaskStopped) -> Self {
        Error::Stopped(error).into()
    }
}

// `sqlite3_error_offset` counts UTF-8 bytes from 0; the contract is a 1-based count of characters
fn char_position(sql: &str, offset: u32) -> Option<u32> {
    let offset = usize::try_from(offset).ok()?;
    let before = sql
        .char_indices()
        .take_while(|(index, _)| *index < offset)
        .count();
    u32::try_from(before.saturating_add(1)).ok()
}

// the only place a SQLite driver error becomes a Python exception
#[must_use]
pub fn to_py_err(py: Python<'_>, failure: &Failure) -> PyErr {
    let (error, position) = match failure {
        Failure::Aborted => {
            return shared::database_error(
                py,
                ABORTED_SQLSTATE,
                ABORTED_MESSAGE,
                None,
                None,
                None,
                Some(ABORTED_CODE),
            );
        }
        Failure::Driver { error, position } => (error, *position),
    };
    match error {
        Error::Sqlite { name, message, .. } => shared::database_error(
            py,
            error.sqlstate().unwrap_or("HY000"),
            message,
            None,
            None,
            position,
            Some(*name).filter(|name| !name.is_empty()),
        ),
        Error::Dsn(_) | Error::Interface(_) | Error::Stopped(_) => {
            shared::interface_error(py, &error.to_string())
        }
        Error::Core(core) => shared::core_to_py_err(py, *core),
        Error::RowShape(_) | Error::Shape { .. } | Error::NullField { .. } => {
            shared::row_shape_error(py, &error.to_string())
        }
        Error::Decode(_) => shared::decode_error(py, &error.to_string()),
        Error::Encode(_) => shared::encode_error(py, &error.to_string()),
    }
}

/// `to_py_err`, except that a named row's mapping and NULL failures are worded with the row type's
/// name and field keys.
#[must_use]
pub fn named_to_py_err(py: Python<'_>, failure: &Failure, shape: &RowShape) -> PyErr {
    match failure {
        Failure::Driver {
            error: Error::Shape { errors, columns },
            ..
        } => shared::named_shape_error(py, shape.data(), errors, columns),
        Failure::Driver {
            error:
                Error::NullField {
                    column,
                    field,
                    row,
                    name,
                },
            ..
        } => shared::named_null_error(py, shape.data(), *field, *column, name, *row),
        other => to_py_err(py, other),
    }
}

/// Carries a failure across an `async` block, where the GIL is not held.
#[derive(Debug)]
pub struct SqliteError(pub Failure);

impl<T: Into<Failure>> From<T> for SqliteError {
    fn from(err: T) -> Self {
        Self(err.into())
    }
}

impl From<SqliteError> for PyErr {
    fn from(err: SqliteError) -> Self {
        Python::attach(|py| to_py_err(py, &err.0))
    }
}
