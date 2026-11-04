use std::pin::Pin;

use bytes::BytesMut;
use futures_util::{FutureExt, TryStreamExt, pin_mut};
use rowx_core::row_shape::{FieldSpec, Mapping, Matching, map_columns};
use tokio_postgres::types::{FromSql, IsNull, ToSql, Type, to_sql_checked};
use tokio_postgres::{Client, Column, Row, RowStream, Statement};

use crate::describe;
use crate::error::Error;
use crate::stmt_cache::StmtCache;

/// The undecoded bytes of one cell (`None` for NULL); accepts every Postgres type.
#[derive(Debug, Clone, Copy)]
pub struct Raw<'a>(pub Option<&'a [u8]>);

impl<'a> FromSql<'a> for Raw<'a> {
    fn from_sql(
        _ty: &Type,
        raw: &'a [u8],
    ) -> Result<Self, Box<dyn std::error::Error + Sync + Send>> {
        Ok(Self(Some(raw)))
    }

    fn from_sql_null(_ty: &Type) -> Result<Self, Box<dyn std::error::Error + Sync + Send>> {
        Ok(Self(None))
    }

    fn accepts(_ty: &Type) -> bool {
        true
    }
}

/// One encoded parameter, ready for Bind. The bytes are never printed, since
/// tokio-postgres logs parameters through `Debug`.
#[derive(Clone)]
pub struct Param {
    value: Option<Vec<u8>>,
    format: tokio_postgres::types::Format,
}

impl Param {
    #[must_use]
    pub const fn null() -> Self {
        Self {
            value: None,
            format: tokio_postgres::types::Format::Binary,
        }
    }

    #[must_use]
    pub const fn binary(value: Vec<u8>) -> Self {
        Self {
            value: Some(value),
            format: tokio_postgres::types::Format::Binary,
        }
    }

    #[must_use]
    pub const fn text(value: Vec<u8>) -> Self {
        Self {
            value: Some(value),
            format: tokio_postgres::types::Format::Text,
        }
    }

    #[must_use]
    pub const fn is_text(&self) -> bool {
        matches!(self.format, tokio_postgres::types::Format::Text)
    }

    #[must_use]
    pub fn value(&self) -> Option<&[u8]> {
        self.value.as_deref()
    }
}

impl std::fmt::Debug for Param {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Param(<redacted>)")
    }
}

impl ToSql for Param {
    fn to_sql(
        &self,
        _ty: &Type,
        out: &mut BytesMut,
    ) -> Result<IsNull, Box<dyn std::error::Error + Sync + Send>> {
        let Some(bytes) = &self.value else {
            return Ok(IsNull::Yes);
        };
        out.extend_from_slice(bytes);
        Ok(IsNull::No)
    }

    fn accepts(_ty: &Type) -> bool {
        true
    }

    fn encode_format(&self, _ty: &Type) -> tokio_postgres::types::Format {
        self.format
    }

    to_sql_checked!();
}

/// Encodes the caller's parameters against the types the server reported for the statement.
pub trait Encode: Sync {
    /// # Errors
    /// `Interface` for a count mismatch, `Encode` when a value does not fit its parameter type.
    fn encode(&self, types: &[Type]) -> Result<Vec<Param>, Error>;
}

impl<F: Fn(&[Type]) -> Result<Vec<Param>, Error> + Sync> Encode for F {
    fn encode(&self, types: &[Type]) -> Result<Vec<Param>, Error> {
        self(types)
    }
}

const TXN_HINT: &str = "schema changed inside a transaction; ROLLBACK and retry";

// the statement for `sql`, and whether it came from the cache
async fn prepare(
    client: &Client,
    cache: &mut StmtCache<Statement>,
    sql: &str,
) -> Result<(Statement, bool), Error> {
    if let Some(stmt) = cache.get(sql) {
        return Ok((stmt, true));
    }
    let stmt = client.prepare(sql).await?;
    reject_impostors(client, &stmt).await?;
    cache.insert(sql, stmt.clone());
    Ok((stmt, false))
}

// the codecs pick an extension type by name, so a same-named type from elsewhere must not get them;
// the owner lookup runs only when a statement mentions such a name, and only on a cache miss
async fn reject_impostors(client: &Client, stmt: &Statement) -> Result<(), Error> {
    let types = || {
        stmt.params()
            .iter()
            .chain(stmt.columns().iter().map(tokio_postgres::Column::type_))
    };
    let candidates = describe::extension_candidates(types());
    if candidates.is_empty() {
        return Ok(());
    }
    let owners = describe::extension_owners(client, &candidates).await?;
    describe::impostor(types(), &owners).map_or(Ok(()), |name| {
        Err(Error::Decode(format!(
            "the type {name} is not owned by the extension that defines a type of that name, \
             so rowx has no codec for it"
        )))
    })
}

enum Retry {
    Revalidate,
    Missing,
}

fn retry_kind(err: &Error) -> Option<Retry> {
    let Error::Db {
        sqlstate, routine, ..
    } = err
    else {
        return None;
    };
    match &**sqlstate {
        "0A000" if routine.as_deref() == Some("RevalidateCachedQuery") => Some(Retry::Revalidate),
        "26000" => Some(Retry::Missing),
        _ => None,
    }
}

fn with_txn_hint(err: Error) -> Error {
    match err {
        Error::Db {
            sqlstate,
            message,
            detail,
            position,
            routine,
            ..
        } => Error::Db {
            sqlstate,
            message,
            detail,
            hint: Some(TXN_HINT.into()),
            position,
            routine,
        },
        other => other,
    }
}

// one retry at most: a stale cached plan (0A000) re-prepares, a vanished statement (26000) clears the cache
#[derive(Default)]
struct Retrier {
    spent: bool,
    original: Option<Error>,
}

impl Retrier {
    // Ok means the caller should run the operation again
    fn on_error(
        &mut self,
        err: Error,
        cache: &mut StmtCache<Statement>,
        sql: &str,
    ) -> Result<(), Error> {
        if self.spent {
            return Err(match self.original.take() {
                // inside a transaction the failed statement aborted it, so the retry cannot succeed
                Some(original) if matches!(&err, Error::Db { sqlstate, .. } if &**sqlstate == "25P02") => {
                    with_txn_hint(original)
                }
                _ => err,
            });
        }
        match retry_kind(&err) {
            Some(Retry::Revalidate) => {
                cache.remove(sql);
                self.original = Some(err);
            }
            Some(Retry::Missing) => cache.clear(),
            None => return Err(err),
        }
        self.spent = true;
        Ok(())
    }
}

/// What the caller expects of the result, checked before and while rows stream in.
#[derive(Debug, Clone, Copy, Default)]
pub struct Shape<'a> {
    /// `true` marks a nullable column, so `false` rejects NULL there. For a named shape (`fields`
    /// set) it is per field instead, and the per-column mask comes from the mapping.
    pub mask: Option<&'a [bool]>,
    pub scalar: bool,
    pub limit_one: bool,
    pub arity: Option<usize>,
    /// The fields of a named row type; the columns are mapped onto them before execution.
    pub fields: Option<&'a [FieldSpec]>,
    pub matching: Matching,
}

/// The rows, and the column-to-field mapping when the shape is named.
pub type Fetched = (Vec<Row>, Option<Mapping>);

/// # Errors
/// `RowShape` for an arity, scalar or NULL-mask violation, `Shape` for a mapping failure,
/// `NullField` for a NULL in a non-nullable mapped field, `Db`/`Interface` for driver failures.
pub async fn fetch(
    client: &Client,
    cache: &mut StmtCache<Statement>,
    sql: &str,
    shape: &Shape<'_>,
    encode: impl Encode,
) -> Result<Fetched, Error> {
    let mut retrier = Retrier::default();
    loop {
        match fetch_once(client, cache, sql, shape, &encode).await {
            Ok(fetched) => return Ok(fetched),
            Err(err) => retrier.on_error(err, cache, sql)?,
        }
    }
}

async fn fetch_once(
    client: &Client,
    cache: &mut StmtCache<Statement>,
    sql: &str,
    shape: &Shape<'_>,
    encode: &impl Encode,
) -> Result<Fetched, Error> {
    let (stream, mapping) = open(client, cache, sql, shape, encode).await?;
    pin_mut!(stream);
    let owned = mapping
        .as_ref()
        .map(|m| column_mask(shape, m))
        .transpose()?;
    let mask = if mapping.is_some() {
        owned.as_deref()
    } else {
        shape.mask
    };
    let mut rows = Vec::new();
    while let Some(row) = stream.try_next().await? {
        if let Some(mask) = mask {
            check_nulls(&row, rows.len(), mask, mapping.as_ref())?;
        }
        rows.push(row);
        if shape.limit_one {
            // read to the end, so the encoding watch has seen the response's last report; a failure
            // after the first row is not this call's
            while let Ok(Some(_)) = stream.try_next().await {}
            break;
        }
    }
    Ok((rows, mapping))
}

// the per-column mask of a named shape: a mapped column takes its field's flag, a pass-through
// column is nullable. `check_shape` guarantees one flag per field, so a miss is a caller bug.
fn column_mask(shape: &Shape<'_>, mapping: &Mapping) -> Result<Vec<bool>, Error> {
    let fields = shape.mask.ok_or_else(missing_field_mask)?;
    mapping
        .columns
        .iter()
        .map(|field| {
            field.map_or(Ok(true), |f| {
                fields.get(f).copied().ok_or_else(missing_field_mask)
            })
        })
        .collect()
}

fn missing_field_mask() -> Error {
    Error::Interface("a named shape needs one nullability flag per field".to_owned())
}

// a named shape must carry a flag for every field, or NULL checks would silently switch off
fn check_field_mask(fields: &[FieldSpec], mask: Option<&[bool]>) -> Result<(), Error> {
    match mask {
        Some(mask) if mask.len() == fields.len() => Ok(()),
        Some(mask) => Err(Error::Interface(format!(
            "the nullability mask covers {} fields, the named shape has {}",
            mask.len(),
            fields.len()
        ))),
        None => Err(missing_field_mask()),
    }
}

// prepare, shape check, encode and send; errors here all precede the first row, so the retrier wraps it
async fn open(
    client: &Client,
    cache: &mut StmtCache<Statement>,
    sql: &str,
    shape: &Shape<'_>,
    encode: &impl Encode,
) -> Result<(RowStream, Option<Mapping>), Error> {
    let (mut stmt, cached) = prepare(client, cache, sql).await?;
    if cached && check_shape(shape, stmt.columns()).is_err() {
        // a schema change since caching can alter the columns, and the server only reports it on
        // execution, which the shape check would prevent; judge the shape on a fresh prepare
        cache.remove(sql);
        (stmt, _) = prepare(client, cache, sql).await?;
    }
    // checked before executing so a statement with no rows still reports a wrong shape
    let mapping = check_shape(shape, stmt.columns())?;
    let params = encode.encode(stmt.params())?;
    Ok((client.query_raw(&stmt, &params).await?, mapping))
}

/// One trip of a stream.
#[derive(Debug)]
pub enum Batch {
    /// At least one row, at most `batch_size`.
    Rows(Vec<Row>),
    /// The stream is over. `rows_affected` is `None` when it ended in an error.
    End { rows_affected: Option<u64> },
}

/// The streaming consumer behind `fetch`-style iteration; dropping it discards the rest of the result.
pub struct RowBatches {
    stream: Option<Pin<Box<RowStream>>>,
    mask: Option<Vec<bool>>,
    mapping: Option<Mapping>,
    batch_size: usize,
    pending: Option<Error>,
    rows_affected: Option<u64>,
    // rows handed out by earlier batches, so a NULL error can name its row
    delivered: usize,
}

impl std::fmt::Debug for RowBatches {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("RowBatches")
            .field("open", &self.stream.is_some())
            .field("batch_size", &self.batch_size)
            .finish_non_exhaustive()
    }
}

impl RowBatches {
    /// The column-to-field mapping of a named shape; fixed for the whole stream.
    #[must_use]
    pub const fn mapping(&self) -> Option<&Mapping> {
        self.mapping.as_ref()
    }

    /// Waits for one row, then takes the rows that are ready without waiting, up to `batch_size`.
    /// A failure after some rows were taken is returned by the next call.
    ///
    /// # Errors
    /// `RowShape` for a NULL-mask violation, `Db`/`Interface` for driver failures.
    pub async fn next_batch(&mut self) -> Result<Batch, Error> {
        if let Some(err) = self.pending.take() {
            return Err(err);
        }
        let Some(stream) = self.stream.as_mut() else {
            return Ok(Batch::End {
                rows_affected: self.rows_affected,
            });
        };
        let mut rows = Vec::new();
        loop {
            let next = if rows.is_empty() {
                stream.try_next().await
            } else {
                match stream.try_next().now_or_never() {
                    Some(next) => next,
                    None => break,
                }
            };
            let err = match next {
                Ok(Some(row)) => {
                    let checked = self.mask.as_deref().map_or(Ok(()), |m| {
                        check_nulls(&row, self.delivered + rows.len(), m, self.mapping.as_ref())
                    });
                    match checked {
                        Ok(()) => {
                            rows.push(row);
                            if rows.len() >= self.batch_size {
                                break;
                            }
                            continue;
                        }
                        Err(err) => err,
                    }
                }
                Ok(None) => {
                    self.rows_affected = stream.rows_affected();
                    self.stream = None;
                    if rows.is_empty() {
                        return Ok(Batch::End {
                            rows_affected: self.rows_affected,
                        });
                    }
                    break;
                }
                Err(err) => err.into(),
            };
            // the rows before the failure go out first
            self.stream = None;
            if rows.is_empty() {
                return Err(err);
            }
            self.pending = Some(err);
            break;
        }
        self.delivered += rows.len();
        Ok(Batch::Rows(rows))
    }
}

/// # Errors
/// Anything `fetch` reports before the first row; later failures come from `next_batch`.
pub async fn stream(
    client: &Client,
    cache: &mut StmtCache<Statement>,
    sql: &str,
    shape: &Shape<'_>,
    encode: impl Encode,
    batch_size: usize,
) -> Result<RowBatches, Error> {
    let mut retrier = Retrier::default();
    let (stream, mapping) = loop {
        match open(client, cache, sql, shape, &encode).await {
            Ok(opened) => break opened,
            Err(err) => retrier.on_error(err, cache, sql)?,
        }
    };
    let mask = mapping.as_ref().map_or_else(
        || Ok(shape.mask.map(<[bool]>::to_vec)),
        |m| column_mask(shape, m).map(Some),
    )?;
    Ok(RowBatches {
        stream: Some(Box::pin(stream)),
        mask,
        mapping,
        batch_size: batch_size.max(1),
        pending: None,
        rows_affected: None,
        delivered: 0,
    })
}

fn check_shape(shape: &Shape<'_>, columns: &[Column]) -> Result<Option<Mapping>, Error> {
    let count = columns.len();
    if let Some(arity) = shape.arity
        && arity != count
    {
        return Err(Error::RowShape(format!(
            "expected {arity} columns, the query returns {count}"
        )));
    }
    if shape.scalar && count != 1 {
        return Err(Error::RowShape(format!(
            "a scalar result needs exactly 1 column, the query returns {count}"
        )));
    }
    // a named shape's mask is per field, so the mapping replaces the column length check
    if let Some(fields) = shape.fields {
        check_field_mask(fields, shape.mask)?;
        let names: Vec<&str> = columns.iter().map(Column::name).collect();
        return map_columns(&names, fields, shape.matching)
            .map(Some)
            .map_err(|errors| Error::Shape {
                errors,
                columns: names.iter().map(|n| (*n).to_owned()).collect(),
            });
    }
    if let Some(mask) = shape.mask
        && mask.len() != count
    {
        return Err(Error::RowShape(format!(
            "the nullability mask covers {} columns, the query returns {count}",
            mask.len()
        )));
    }
    Ok(None)
}

/// # Errors
/// `Db`/`Interface` for driver failures, `Encode` for a parameter that does not fit.
pub async fn execute(
    client: &Client,
    cache: &mut StmtCache<Statement>,
    sql: &str,
    encode: impl Encode,
) -> Result<u64, Error> {
    let mut retrier = Retrier::default();
    loop {
        match execute_once(client, cache, sql, &encode).await {
            Ok(count) => return Ok(count),
            Err(err) => retrier.on_error(err, cache, sql)?,
        }
    }
}

async fn execute_once(
    client: &Client,
    cache: &mut StmtCache<Statement>,
    sql: &str,
    encode: &impl Encode,
) -> Result<u64, Error> {
    let (stmt, _) = prepare(client, cache, sql).await?;
    let params = encode.encode(stmt.params())?;
    Ok(client.execute_raw(&stmt, &params).await?)
}

fn check_nulls(
    row: &Row,
    row_index: usize,
    mask: &[bool],
    mapping: Option<&Mapping>,
) -> Result<(), Error> {
    for (index, nullable) in mask.iter().enumerate() {
        if !nullable && row.try_get::<_, Raw>(index)?.0.is_none() {
            // a named shape reports the field too, so the binding can render its message
            if let Some(field) = mapping.and_then(|m| m.columns.get(index).copied().flatten()) {
                return Err(Error::NullField {
                    column: index,
                    field,
                    row: row_index,
                    name: row
                        .columns()
                        .get(index)
                        .map_or_else(Box::default, |c| c.name().into()),
                });
            }
            return Err(Error::RowShape(format!(
                "NULL in a non-nullable column at index {index}"
            )));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use rowx_core::row_shape::{FieldSpec, Names};

    use super::{Error, check_field_mask};

    fn fields(count: usize) -> Vec<FieldSpec> {
        (0..count)
            .map(|_| FieldSpec {
                names: Names::Unknown,
                required: false,
            })
            .collect()
    }

    #[test]
    fn field_mask_must_cover_every_field() {
        assert_eq!(check_field_mask(&fields(2), Some(&[true, false])), Ok(()));
        assert!(matches!(
            check_field_mask(&fields(2), Some(&[true])),
            Err(Error::Interface(_))
        ));
        assert!(matches!(
            check_field_mask(&fields(2), Some(&[true, false, true])),
            Err(Error::Interface(_))
        ));
        assert!(matches!(
            check_field_mask(&fields(2), None),
            Err(Error::Interface(_))
        ));
    }
}
