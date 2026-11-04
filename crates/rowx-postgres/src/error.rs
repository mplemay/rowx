use rowx_core::error::{CoreError, TaskStopped};
use rowx_core::row_shape::ShapeErrors;
use thiserror::Error;
use tokio_postgres::error::ErrorPosition;

/// What the session changed that rowx cannot encode for.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum Drift {
    #[error("the session changed client_encoding to {0}; rowx requires UTF8")]
    ClientEncoding(Box<str>),
    #[error("the session turned standard_conforming_strings off; rowx requires on")]
    StandardConformingStrings,
}

#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum Error {
    #[error(transparent)]
    Drift(#[from] Drift),
    #[error("{0}")]
    Interface(String),
    #[error("{sqlstate}: {message}")]
    Db {
        // boxed strs keep the enum small enough for clippy::result_large_err
        sqlstate: Box<str>,
        message: Box<str>,
        detail: Option<Box<str>>,
        hint: Option<Box<str>>,
        position: Option<u32>,
        routine: Option<Box<str>>,
    },
    #[error(transparent)]
    Core(#[from] CoreError),
    #[error("{0}")]
    RowShape(String),
    /// A named row's columns do not map onto its fields; the binding renders the text from the indices
    /// and the raw column names.
    #[error("{errors}")]
    Shape {
        errors: ShapeErrors,
        columns: Vec<String>,
    },
    /// NULL in a mapped column whose field is not nullable. The indices are 0-based, `name` is the
    /// raw column name, and `row` counts the rows delivered so far.
    #[error("NULL in non-nullable field {field} (column {column} {name:?}, row {row})")]
    NullField {
        column: usize,
        field: usize,
        row: usize,
        name: Box<str>,
    },
    #[error("{0}")]
    Decode(String),
    #[error("{0}")]
    Encode(String),
}

impl From<tokio_postgres::Error> for Error {
    fn from(err: tokio_postgres::Error) -> Self {
        if let Some(db) = err.as_db_error() {
            return Self::Db {
                sqlstate: db.code().code().into(),
                message: db.message().into(),
                detail: db.detail().map(Into::into),
                hint: db.hint().map(Into::into),
                position: db.position().map(|p| match p {
                    ErrorPosition::Original(n) => *n,
                    ErrorPosition::Internal { position, .. } => *position,
                }),
                routine: db.routine().map(Into::into),
            };
        }
        if err.is_closed() {
            return Self::Core(CoreError::Closed);
        }
        let text = err.to_string();
        // the kind is private; its text is fixed, and its cause says why the client refused (for example
        // that the server did not use channel binding). `connect_parsed` redacts the whole message.
        if text == "authentication error"
            && let Some(cause) = std::error::Error::source(&err)
        {
            return Self::Interface(format!("{text}: {cause}"));
        }
        Self::Interface(text)
    }
}

impl From<TaskStopped> for Error {
    fn from(err: TaskStopped) -> Self {
        Self::Interface(err.to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::Error;

    #[test]
    fn null_field_display_names_the_field_and_column() {
        assert_eq!(
            Error::NullField {
                column: 3,
                field: 1,
                row: 4,
                name: "email".into()
            }
            .to_string(),
            "NULL in non-nullable field 1 (column 3 \"email\", row 4)"
        );
    }
}
