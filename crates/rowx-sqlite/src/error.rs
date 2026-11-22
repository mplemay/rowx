use rowx_core::error::{CoreError, TaskStopped};
use rowx_core::row_shape::{NullCheckError, NullError, ShapeCheckError, ShapeErrors};
use rusqlite::ffi;
use thiserror::Error;

use crate::dsn::DsnError;
use crate::value::{DecodeError, EncodeError};

#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum Error {
    #[error(transparent)]
    Dsn(#[from] DsnError),
    #[error("{0}")]
    Interface(String),
    /// SQL that `describe` cannot take: it holds no statement, or more than one. A problem with the
    /// SQL, not with the connection.
    #[error("{0}")]
    NotOneStatement(String),
    /// A failure SQLite reported. `code` is the extended result code, `name` its constant
    /// (`SQLITE_CONSTRAINT_UNIQUE`), and `offset` the byte offset into the SQL that
    /// `sqlite3_error_offset` pointed at.
    #[error("{name}: {message}")]
    Sqlite {
        code: i32,
        name: &'static str,
        message: Box<str>,
        offset: Option<u32>,
    },
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
    #[error(transparent)]
    Decode(#[from] DecodeError),
    #[error(transparent)]
    Encode(#[from] EncodeError),
    /// A shared connection or pool error; this driver produces `Closed`.
    #[error(transparent)]
    Core(#[from] CoreError),
    /// The worker thread ended before it answered, so the connection is broken.
    #[error(transparent)]
    Stopped(#[from] TaskStopped),
}

impl Error {
    #[must_use]
    pub fn from_code(code: i32, message: impl Into<Box<str>>, offset: Option<u32>) -> Self {
        Self::Sqlite {
            code,
            name: code_name(code),
            message: message.into(),
            offset,
        }
    }

    /// `true` for a statement that SQLite interrupted (`SQLITE_INTERRUPT`, whatever the extended code).
    #[must_use]
    pub fn is_interrupt(&self) -> bool {
        matches!(self, Self::Sqlite { code, .. } if code & 0xff == ffi::SQLITE_INTERRUPT)
    }

    /// `true` for a lock that could not be taken (`SQLITE_BUSY` or `SQLITE_LOCKED`, whatever the extended code).
    #[must_use]
    pub fn is_busy(&self) -> bool {
        matches!(self, Self::Sqlite { code, .. }
            if matches!(code & 0xff, ffi::SQLITE_BUSY | ffi::SQLITE_LOCKED))
    }

    /// The SQLSTATE of a SQLite failure; other errors have none.
    #[must_use]
    pub const fn sqlstate(&self) -> Option<&'static str> {
        match self {
            Self::Sqlite { code, .. } => Some(sqlstate(*code)),
            _ => None,
        }
    }
}

impl From<rusqlite::Error> for Error {
    fn from(err: rusqlite::Error) -> Self {
        match err {
            rusqlite::Error::SqliteFailure(failure, message) => {
                let message = message.unwrap_or_else(|| failure.to_string());
                Self::from_code(failure.extended_code, message, None)
            }
            rusqlite::Error::SqlInputError {
                error, msg, offset, ..
            } => Self::from_code(error.extended_code, msg, u32::try_from(offset).ok()),
            other => Self::Interface(other.to_string()),
        }
    }
}

impl From<ShapeCheckError> for Error {
    fn from(err: ShapeCheckError) -> Self {
        match err {
            ShapeCheckError::RowShape(text) => Self::RowShape(text),
            ShapeCheckError::Mapping { errors, columns } => Self::Shape { errors, columns },
            ShapeCheckError::Interface(text) => Self::Interface(text),
        }
    }
}

impl From<NullError> for Error {
    fn from(err: NullError) -> Self {
        match err {
            NullError::Column { .. } => Self::RowShape(err.to_string()),
            NullError::Field {
                column,
                field,
                row,
                name,
            } => Self::NullField {
                column,
                field,
                row,
                name,
            },
        }
    }
}

impl From<NullCheckError<Self>> for Error {
    fn from(err: NullCheckError<Self>) -> Self {
        match err {
            NullCheckError::Null(null) => null.into(),
            NullCheckError::Read(read) => read,
        }
    }
}

macro_rules! names {
    ($code:expr, $($name:ident),+ $(,)?) => {
        match $code {
            $(ffi::$name => stringify!($name),)+
            _ => "",
        }
    };
}

const fn primary_name(code: i32) -> &'static str {
    names!(
        code,
        SQLITE_ERROR,
        SQLITE_INTERNAL,
        SQLITE_PERM,
        SQLITE_ABORT,
        SQLITE_BUSY,
        SQLITE_LOCKED,
        SQLITE_NOMEM,
        SQLITE_READONLY,
        SQLITE_INTERRUPT,
        SQLITE_IOERR,
        SQLITE_CORRUPT,
        SQLITE_NOTFOUND,
        SQLITE_FULL,
        SQLITE_CANTOPEN,
        SQLITE_PROTOCOL,
        SQLITE_EMPTY,
        SQLITE_SCHEMA,
        SQLITE_TOOBIG,
        SQLITE_CONSTRAINT,
        SQLITE_MISMATCH,
        SQLITE_MISUSE,
        SQLITE_NOLFS,
        SQLITE_AUTH,
        SQLITE_FORMAT,
        SQLITE_RANGE,
        SQLITE_NOTADB,
        SQLITE_NOTICE,
        SQLITE_WARNING,
    )
}

const fn io_name(code: i32) -> &'static str {
    names!(
        code,
        SQLITE_IOERR_READ,
        SQLITE_IOERR_SHORT_READ,
        SQLITE_IOERR_WRITE,
        SQLITE_IOERR_FSYNC,
        SQLITE_IOERR_DIR_FSYNC,
        SQLITE_IOERR_TRUNCATE,
        SQLITE_IOERR_FSTAT,
        SQLITE_IOERR_UNLOCK,
        SQLITE_IOERR_RDLOCK,
        SQLITE_IOERR_DELETE,
        SQLITE_IOERR_BLOCKED,
        SQLITE_IOERR_NOMEM,
        SQLITE_IOERR_ACCESS,
        SQLITE_IOERR_CHECKRESERVEDLOCK,
        SQLITE_IOERR_LOCK,
        SQLITE_IOERR_CLOSE,
        SQLITE_IOERR_DIR_CLOSE,
        SQLITE_IOERR_SHMOPEN,
        SQLITE_IOERR_SHMSIZE,
        SQLITE_IOERR_SHMLOCK,
        SQLITE_IOERR_SHMMAP,
        SQLITE_IOERR_SEEK,
        SQLITE_IOERR_DELETE_NOENT,
        SQLITE_IOERR_MMAP,
        SQLITE_IOERR_GETTEMPPATH,
        SQLITE_IOERR_CONVPATH,
        SQLITE_IOERR_VNODE,
        SQLITE_IOERR_AUTH,
        SQLITE_IOERR_BEGIN_ATOMIC,
        SQLITE_IOERR_COMMIT_ATOMIC,
        SQLITE_IOERR_ROLLBACK_ATOMIC,
        SQLITE_IOERR_DATA,
        SQLITE_IOERR_CORRUPTFS,
        SQLITE_IOERR_IN_PAGE,
        SQLITE_IOERR_BADKEY,
        SQLITE_IOERR_CODEC,
    )
}

const fn extended_name(code: i32) -> &'static str {
    names!(
        code,
        SQLITE_ERROR_MISSING_COLLSEQ,
        SQLITE_ERROR_RETRY,
        SQLITE_ERROR_SNAPSHOT,
        SQLITE_ERROR_RESERVESIZE,
        SQLITE_ERROR_KEY,
        SQLITE_ERROR_UNABLE,
        SQLITE_LOCKED_SHAREDCACHE,
        SQLITE_LOCKED_VTAB,
        SQLITE_BUSY_RECOVERY,
        SQLITE_BUSY_SNAPSHOT,
        SQLITE_BUSY_TIMEOUT,
        SQLITE_CANTOPEN_NOTEMPDIR,
        SQLITE_CANTOPEN_ISDIR,
        SQLITE_CANTOPEN_FULLPATH,
        SQLITE_CANTOPEN_CONVPATH,
        SQLITE_CANTOPEN_DIRTYWAL,
        SQLITE_CANTOPEN_SYMLINK,
        SQLITE_CORRUPT_VTAB,
        SQLITE_CORRUPT_SEQUENCE,
        SQLITE_CORRUPT_INDEX,
        SQLITE_READONLY_RECOVERY,
        SQLITE_READONLY_CANTLOCK,
        SQLITE_READONLY_ROLLBACK,
        SQLITE_READONLY_DBMOVED,
        SQLITE_READONLY_CANTINIT,
        SQLITE_READONLY_DIRECTORY,
        SQLITE_ABORT_ROLLBACK,
        SQLITE_CONSTRAINT_CHECK,
        SQLITE_CONSTRAINT_COMMITHOOK,
        SQLITE_CONSTRAINT_FOREIGNKEY,
        SQLITE_CONSTRAINT_FUNCTION,
        SQLITE_CONSTRAINT_NOTNULL,
        SQLITE_CONSTRAINT_PRIMARYKEY,
        SQLITE_CONSTRAINT_TRIGGER,
        SQLITE_CONSTRAINT_UNIQUE,
        SQLITE_CONSTRAINT_VTAB,
        SQLITE_CONSTRAINT_ROWID,
        SQLITE_CONSTRAINT_PINNED,
        SQLITE_CONSTRAINT_DATATYPE,
        SQLITE_NOTICE_RECOVER_WAL,
        SQLITE_NOTICE_RECOVER_ROLLBACK,
        SQLITE_NOTICE_RBU,
        SQLITE_WARNING_AUTOINDEX,
        SQLITE_AUTH_USER,
    )
}

const fn exact_name(code: i32) -> &'static str {
    let primary = primary_name(code);
    if !primary.is_empty() {
        return primary;
    }
    let io = io_name(code);
    if !io.is_empty() {
        return io;
    }
    extended_name(code)
}

/// The constant name of an extended result code. A code this table lacks takes the name of its primary
/// code, and `SQLITE_UNKNOWN` when that is unknown too.
#[must_use]
pub const fn code_name(code: i32) -> &'static str {
    let exact = exact_name(code);
    if !exact.is_empty() {
        return exact;
    }
    let primary = exact_name(code & 0xff);
    if primary.is_empty() {
        "SQLITE_UNKNOWN"
    } else {
        primary
    }
}

/// Maps an extended result code onto the SQLSTATE the shared exception registry keys on.
#[must_use]
pub const fn sqlstate(code: i32) -> &'static str {
    match code {
        ffi::SQLITE_CONSTRAINT_PRIMARYKEY
        | ffi::SQLITE_CONSTRAINT_UNIQUE
        | ffi::SQLITE_CONSTRAINT_ROWID => "23505",
        ffi::SQLITE_CONSTRAINT_NOTNULL => "23502",
        ffi::SQLITE_CONSTRAINT_FOREIGNKEY => "23503",
        ffi::SQLITE_CONSTRAINT_CHECK => "23514",
        ffi::SQLITE_CONSTRAINT_DATATYPE => "22000",
        ffi::SQLITE_BUSY_SNAPSHOT => "40001",
        _ => match code & 0xff {
            ffi::SQLITE_CONSTRAINT => "23000",
            ffi::SQLITE_MISMATCH => "22000",
            ffi::SQLITE_TOOBIG => "54000",
            ffi::SQLITE_BUSY | ffi::SQLITE_LOCKED => "55P03",
            ffi::SQLITE_INTERRUPT => "57014",
            ffi::SQLITE_READONLY => "25006",
            ffi::SQLITE_FULL => "53100",
            ffi::SQLITE_CANTOPEN => "58030",
            ffi::SQLITE_CORRUPT => "XX001",
            ffi::SQLITE_NOTADB => "XX002",
            _ => "HY000",
        },
    }
}

#[cfg(test)]
mod tests {
    use super::{Error, code_name, sqlstate};
    use crate::dsn::DsnError;
    use crate::value::{DecodeError, EncodeError};
    use rowx_core::row_shape::{NullCheckError, NullError, ShapeCheckError};
    use rusqlite::ffi;

    #[test]
    fn only_interrupt_class_codes_are_interrupts() {
        assert!(Error::from_code(ffi::SQLITE_INTERRUPT, "interrupted", None).is_interrupt());
        assert!(!Error::from_code(ffi::SQLITE_CONSTRAINT_UNIQUE, "unique", None).is_interrupt());
        assert!(!Error::from_code(ffi::SQLITE_BUSY, "busy", None).is_interrupt());
        assert!(!Error::Interface("x".into()).is_interrupt());
    }

    #[test]
    fn sqlstate_table() {
        let cases: [(i32, &str); 52] = [
            (ffi::SQLITE_CONSTRAINT_PRIMARYKEY, "23505"),
            (ffi::SQLITE_CONSTRAINT_UNIQUE, "23505"),
            (ffi::SQLITE_CONSTRAINT_ROWID, "23505"),
            (ffi::SQLITE_CONSTRAINT_NOTNULL, "23502"),
            (ffi::SQLITE_CONSTRAINT_FOREIGNKEY, "23503"),
            (ffi::SQLITE_CONSTRAINT_CHECK, "23514"),
            (ffi::SQLITE_CONSTRAINT_DATATYPE, "22000"),
            (ffi::SQLITE_MISMATCH, "22000"),
            (ffi::SQLITE_CONSTRAINT, "23000"),
            (ffi::SQLITE_CONSTRAINT_TRIGGER, "23000"),
            (ffi::SQLITE_CONSTRAINT_FUNCTION, "23000"),
            (ffi::SQLITE_CONSTRAINT_COMMITHOOK, "23000"),
            (ffi::SQLITE_CONSTRAINT_VTAB, "23000"),
            (ffi::SQLITE_CONSTRAINT_PINNED, "23000"),
            (ffi::SQLITE_CONSTRAINT | (99 << 8), "23000"),
            (ffi::SQLITE_TOOBIG, "54000"),
            (ffi::SQLITE_BUSY, "55P03"),
            (ffi::SQLITE_BUSY_RECOVERY, "55P03"),
            (ffi::SQLITE_BUSY_TIMEOUT, "55P03"),
            (ffi::SQLITE_LOCKED, "55P03"),
            (ffi::SQLITE_LOCKED_SHAREDCACHE, "55P03"),
            (ffi::SQLITE_LOCKED_VTAB, "55P03"),
            (ffi::SQLITE_BUSY_SNAPSHOT, "40001"),
            (ffi::SQLITE_INTERRUPT, "57014"),
            (ffi::SQLITE_READONLY, "25006"),
            (ffi::SQLITE_READONLY_RECOVERY, "25006"),
            (ffi::SQLITE_READONLY_DBMOVED, "25006"),
            (ffi::SQLITE_READONLY_DIRECTORY, "25006"),
            (ffi::SQLITE_FULL, "53100"),
            (ffi::SQLITE_CANTOPEN, "58030"),
            (ffi::SQLITE_CANTOPEN_ISDIR, "58030"),
            (ffi::SQLITE_CANTOPEN_FULLPATH, "58030"),
            (ffi::SQLITE_CORRUPT, "XX001"),
            (ffi::SQLITE_CORRUPT_INDEX, "XX001"),
            (ffi::SQLITE_CORRUPT_VTAB, "XX001"),
            (ffi::SQLITE_NOTADB, "XX002"),
            (ffi::SQLITE_ERROR, "HY000"),
            (ffi::SQLITE_ERROR_MISSING_COLLSEQ, "HY000"),
            (ffi::SQLITE_INTERNAL, "HY000"),
            (ffi::SQLITE_PERM, "HY000"),
            (ffi::SQLITE_ABORT, "HY000"),
            (ffi::SQLITE_ABORT_ROLLBACK, "HY000"),
            (ffi::SQLITE_NOMEM, "HY000"),
            (ffi::SQLITE_IOERR, "HY000"),
            (ffi::SQLITE_IOERR_READ, "HY000"),
            (ffi::SQLITE_MISUSE, "HY000"),
            (ffi::SQLITE_RANGE, "HY000"),
            (ffi::SQLITE_AUTH, "HY000"),
            (ffi::SQLITE_SCHEMA, "HY000"),
            (ffi::SQLITE_FORMAT, "HY000"),
            (99_999, "HY000"),
            (-1, "HY000"),
        ];
        for (code, expected) in cases {
            assert_eq!(sqlstate(code), expected, "{code} {}", code_name(code));
        }
    }

    #[test]
    fn code_names_are_the_extended_names() {
        let cases = [
            (ffi::SQLITE_CONSTRAINT_UNIQUE, "SQLITE_CONSTRAINT_UNIQUE"),
            (
                ffi::SQLITE_CONSTRAINT_PRIMARYKEY,
                "SQLITE_CONSTRAINT_PRIMARYKEY",
            ),
            (
                ffi::SQLITE_CONSTRAINT_DATATYPE,
                "SQLITE_CONSTRAINT_DATATYPE",
            ),
            (ffi::SQLITE_CONSTRAINT, "SQLITE_CONSTRAINT"),
            (ffi::SQLITE_BUSY_SNAPSHOT, "SQLITE_BUSY_SNAPSHOT"),
            (ffi::SQLITE_READONLY_DBMOVED, "SQLITE_READONLY_DBMOVED"),
            (ffi::SQLITE_ERROR, "SQLITE_ERROR"),
            (ffi::SQLITE_MISMATCH, "SQLITE_MISMATCH"),
            // an unlisted extended code falls back to its primary code
            (ffi::SQLITE_CONSTRAINT | (99 << 8), "SQLITE_CONSTRAINT"),
            (99_999, "SQLITE_UNKNOWN"),
            (-1, "SQLITE_UNKNOWN"),
        ];
        for (code, expected) in cases {
            assert_eq!(code_name(code), expected, "{code}");
        }
    }

    #[test]
    fn later_extended_names_resolve_to_themselves() {
        macro_rules! check {
            ($($name:ident),+ $(,)?) => {
                $(assert_eq!(code_name(ffi::$name), stringify!($name));)+
            };
        }
        check!(
            SQLITE_CONSTRAINT_DATATYPE,
            SQLITE_ERROR_RESERVESIZE,
            SQLITE_ERROR_KEY,
            SQLITE_ERROR_UNABLE,
            SQLITE_IOERR_IN_PAGE,
            SQLITE_IOERR_BADKEY,
            SQLITE_IOERR_CODEC,
            SQLITE_NOTICE_RBU,
        );
    }

    #[test]
    fn sqlite_failure_keeps_code_name_and_message() {
        let err = Error::from(rusqlite::Error::SqliteFailure(
            ffi::Error::new(ffi::SQLITE_CONSTRAINT_UNIQUE),
            Some("UNIQUE constraint failed: t.a".to_owned()),
        ));
        assert_eq!(
            err,
            Error::Sqlite {
                code: ffi::SQLITE_CONSTRAINT_UNIQUE,
                name: "SQLITE_CONSTRAINT_UNIQUE",
                message: "UNIQUE constraint failed: t.a".into(),
                offset: None,
            }
        );
        assert_eq!(err.sqlstate(), Some("23505"));
        assert_eq!(
            err.to_string(),
            "SQLITE_CONSTRAINT_UNIQUE: UNIQUE constraint failed: t.a"
        );
    }

    #[test]
    fn sqlite_failure_without_a_message_uses_the_code_description() {
        let err = Error::from(rusqlite::Error::SqliteFailure(
            ffi::Error::new(ffi::SQLITE_BUSY),
            None,
        ));
        assert!(matches!(
            &err,
            Error::Sqlite { code, name: "SQLITE_BUSY", message, .. }
                if *code == ffi::SQLITE_BUSY && !message.is_empty()
        ));
    }

    #[test]
    fn sql_input_error_carries_the_offset() {
        let err = Error::from(rusqlite::Error::SqlInputError {
            error: ffi::Error::new(ffi::SQLITE_ERROR),
            msg: "near \"selec\": syntax error".to_owned(),
            sql: "selec 1".to_owned(),
            offset: 0,
        });
        assert_eq!(
            err,
            Error::Sqlite {
                code: ffi::SQLITE_ERROR,
                name: "SQLITE_ERROR",
                message: "near \"selec\": syntax error".into(),
                offset: Some(0),
            }
        );
        assert_eq!(err.sqlstate(), Some("HY000"));
    }

    #[test]
    fn other_rusqlite_errors_are_interface_errors() {
        let err = Error::from(rusqlite::Error::MultipleStatement);
        assert!(matches!(err, Error::Interface(_)));
        assert_eq!(err.sqlstate(), None);
    }

    #[test]
    fn wrapped_errors_convert_and_display() {
        assert!(matches!(
            Error::from(DsnError::EmptyPath),
            Error::Dsn(DsnError::EmptyPath)
        ));
        assert_eq!(
            Error::from(DecodeError::new("bad decode")).to_string(),
            "bad decode"
        );
        assert_eq!(
            Error::from(EncodeError::new("bad encode")).to_string(),
            "bad encode"
        );
        assert_eq!(
            Error::from(ShapeCheckError::RowShape("arity".to_owned())),
            Error::RowShape("arity".to_owned())
        );
    }

    #[test]
    fn null_field_display_names_the_field_and_column() {
        let err = Error::from(NullError::Field {
            column: 3,
            field: 1,
            row: 4,
            name: "email".into(),
        });
        assert_eq!(
            err.to_string(),
            "NULL in non-nullable field 1 (column 3 \"email\", row 4)"
        );
        assert!(matches!(
            Error::from(NullCheckError::Null(NullError::Column { index: 2 })),
            Error::RowShape(_)
        ));
    }
}
