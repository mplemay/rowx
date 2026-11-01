use thiserror::Error;

// NAMEDATALEN - 1: the server silently truncates longer identifiers
const MAX_IDENT_BYTES: usize = 63;

#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum NameError {
    #[error("the {kind} name {input:?} is empty")]
    Empty { kind: &'static str, input: String },
    #[error("the table name {input:?} has {parts} parts; use `name` or `schema.name`")]
    TooManyParts { input: String, parts: usize },
    #[error("the schema name {input:?} must be a single identifier, not `schema.name`")]
    QualifiedSchema { input: String },
    #[error(
        "the {kind} name {input:?} is not a valid identifier: an unquoted part must match \
         [A-Za-z_][A-Za-z0-9_$]*, and anything else needs double quotes"
    )]
    Invalid { kind: &'static str, input: String },
    #[error("the {kind} name {input:?} has an unterminated or misplaced double quote")]
    Quote { kind: &'static str, input: String },
    #[error("the {kind} name {input:?} contains a NUL byte")]
    Nul { kind: &'static str, input: String },
    #[error("the {kind} name {input:?} is longer than {MAX_IDENT_BYTES} bytes")]
    TooLong { kind: &'static str, input: String },
}

// One identifier part after folding and unquoting, ready to be quoted again
fn quote(part: &str) -> String {
    format!("\"{}\"", part.replace('"', "\"\""))
}

fn is_unquoted(part: &str) -> bool {
    let mut bytes = part.bytes();
    bytes
        .next()
        .is_some_and(|b| b.is_ascii_alphabetic() || b == b'_')
        && bytes.all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'$')
}

// Splits on dots outside double quotes and resolves each part the way the server does: an
// unquoted part is lowercased, a quoted part is verbatim with `""` read as `"`
fn parse_parts(input: &str, kind: &'static str) -> Result<Vec<String>, NameError> {
    let invalid = || NameError::Invalid {
        kind,
        input: input.to_owned(),
    };
    let quote_error = || NameError::Quote {
        kind,
        input: input.to_owned(),
    };
    if input.contains('\0') {
        return Err(NameError::Nul {
            kind,
            input: input.to_owned(),
        });
    }
    let mut parts = Vec::new();
    let mut rest = input;
    loop {
        let (part, tail) = if let Some(quoted) = rest.strip_prefix('"') {
            let mut value = String::new();
            let mut chars = quoted.char_indices();
            let end = loop {
                match chars.next() {
                    None => return Err(quote_error()),
                    Some((i, '"')) => {
                        if quoted[i + 1..].starts_with('"') {
                            value.push('"');
                            chars.next();
                        } else {
                            break i + 1;
                        }
                    }
                    Some((_, c)) => value.push(c),
                }
            };
            (value, &quoted[end..])
        } else {
            let end = rest.find('.').unwrap_or(rest.len());
            let raw = &rest[..end];
            if raw.contains('"') {
                return Err(quote_error());
            }
            if !is_unquoted(raw) {
                return Err(if raw.is_empty() {
                    NameError::Empty {
                        kind,
                        input: input.to_owned(),
                    }
                } else {
                    invalid()
                });
            }
            (raw.to_ascii_lowercase(), &rest[end..])
        };
        if part.is_empty() {
            return Err(NameError::Empty {
                kind,
                input: input.to_owned(),
            });
        }
        if part.len() > MAX_IDENT_BYTES {
            return Err(NameError::TooLong {
                kind,
                input: input.to_owned(),
            });
        }
        parts.push(part);
        match tail.strip_prefix('.') {
            Some(next) => rest = next,
            None if tail.is_empty() => return Ok(parts),
            None => return Err(quote_error()),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SchemaName(String);

impl SchemaName {
    /// Parses one identifier with Postgres rules: unquoted parts are lowercased, `"…"` parts
    /// are verbatim, and each is at most 63 bytes.
    ///
    /// # Errors
    /// A [`NameError`] for an empty, qualified, malformed or too long name.
    pub fn parse(input: &str) -> Result<Self, NameError> {
        let mut parts = parse_parts(input, "schema")?;
        match (parts.pop(), parts.is_empty()) {
            (Some(name), true) => Ok(Self(name)),
            _ => Err(NameError::QualifiedSchema {
                input: input.to_owned(),
            }),
        }
    }

    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }

    // The identifier as SQL, always double quoted
    #[must_use]
    pub fn quoted(&self) -> String {
        quote(&self.0)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TableName {
    schema: Option<String>,
    name: String,
}

impl TableName {
    /// Parses `name` or `schema.name` with Postgres identifier rules: an unquoted part matches
    /// `[A-Za-z_][A-Za-z0-9_$]*` and is lowercased, a `"…"` part is verbatim with `""` read as
    /// `"`, and each part is at most 63 bytes.
    ///
    /// # Errors
    /// A [`NameError`] for an empty, malformed, too long or three-part name.
    pub fn parse(input: &str) -> Result<Self, NameError> {
        let mut parts = parse_parts(input, "table")?;
        let parts_len = parts.len();
        match (parts.pop(), parts.pop(), parts.is_empty()) {
            (Some(name), None, _) => Ok(Self { schema: None, name }),
            (Some(name), Some(schema), true) => Ok(Self {
                schema: Some(schema),
                name,
            }),
            _ => Err(NameError::TooManyParts {
                input: input.to_owned(),
                parts: parts_len,
            }),
        }
    }

    #[must_use]
    pub fn schema(&self) -> Option<&str> {
        self.schema.as_deref()
    }

    #[must_use]
    pub fn name(&self) -> &str {
        &self.name
    }

    // The name as SQL, each part double quoted
    #[must_use]
    pub fn quoted(&self) -> String {
        self.schema.as_ref().map_or_else(
            || quote(&self.name),
            |schema| format!("{}.{}", quote(schema), quote(&self.name)),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::{NameError, SchemaName, TableName};

    fn table(input: &str) -> TableName {
        TableName::parse(input).unwrap_or_else(|e| unreachable!("{input:?}: {e}"))
    }

    #[test]
    fn a_plain_name_is_one_quoted_part() {
        let name = table("_rowx_migrations");
        assert_eq!(name.schema(), None);
        assert_eq!(name.name(), "_rowx_migrations");
        assert_eq!(name.quoted(), "\"_rowx_migrations\"");
    }

    #[test]
    fn unquoted_parts_fold_to_lowercase() {
        let name = table("MySchema.Migrations");
        assert_eq!(name.schema(), Some("myschema"));
        assert_eq!(name.name(), "migrations");
        assert_eq!(name.quoted(), "\"myschema\".\"migrations\"");
        assert_eq!(table("a$1.B_2").quoted(), "\"a$1\".\"b_2\"");
    }

    #[test]
    fn quoted_parts_are_verbatim() {
        let name = table("\"My\".\"T\"");
        assert_eq!((name.schema(), name.name()), (Some("My"), "T"));
        assert_eq!(name.quoted(), "\"My\".\"T\"");
        // a quoted part may hold anything, dots and spaces included
        assert_eq!(table("\"my app.x\"").name(), "my app.x");
        assert_eq!(table("\"my app\".history").schema(), Some("my app"));
    }

    #[test]
    fn an_embedded_doubled_quote_is_unescaped_and_requoted() {
        let name = table("\"a\"\"b\"");
        assert_eq!(name.name(), "a\"b");
        assert_eq!(name.quoted(), "\"a\"\"b\"");
        assert_eq!(table("\"\"\"\"").name(), "\"");
    }

    #[test]
    fn three_parts_are_rejected() {
        assert!(matches!(
            TableName::parse("a.b.c"),
            Err(NameError::TooManyParts { parts: 3, .. })
        ));
        assert!(TableName::parse("\"a\".\"b\".\"c\"").is_err());
    }

    #[test]
    fn a_part_is_at_most_63_bytes() {
        assert!(TableName::parse(&"x".repeat(63)).is_ok());
        assert!(matches!(
            TableName::parse(&"x".repeat(64)),
            Err(NameError::TooLong { .. })
        ));
        assert!(TableName::parse(&format!("\"{}\"", "x".repeat(64))).is_err());
        // the limit is in bytes of the unquoted value, so 32 two-byte characters are too long
        assert!(TableName::parse(&format!("\"{}\"", "é".repeat(32))).is_err());
        assert!(TableName::parse(&format!("{}.{}", "x".repeat(63), "y".repeat(64))).is_err());
    }

    #[test]
    fn empty_parts_are_rejected() {
        for bad in ["", ".m", "a.", "a..b", "\"\"", "a.\"\"", "\"\".a"] {
            assert!(
                matches!(TableName::parse(bad), Err(NameError::Empty { .. })),
                "{bad:?}"
            );
        }
    }

    #[test]
    fn other_malformed_names_are_rejected() {
        for bad in [
            "1abc", "a b", "a-b", "$a", "é", " a", "a\0b", "\"a", "\"a\"b", "a\"b", "\"a\"\"",
        ] {
            assert!(TableName::parse(bad).is_err(), "{bad:?}");
        }
    }

    #[test]
    fn a_schema_is_a_single_identifier() {
        assert_eq!(
            SchemaName::parse("MyApp").map(|s| s.as_str().to_owned()),
            Ok("myapp".to_owned())
        );
        let quoted = SchemaName::parse("\"my app\"").unwrap_or_else(|e| unreachable!("{e}"));
        assert_eq!(quoted.quoted(), "\"my app\"");
        assert!(matches!(
            SchemaName::parse("a.b"),
            Err(NameError::QualifiedSchema { .. })
        ));
        for bad in ["", "my app", &"x".repeat(64)] {
            assert!(SchemaName::parse(bad).is_err(), "{bad:?}");
        }
    }

    #[test]
    fn errors_name_the_input() {
        let message = TableName::parse("a.b.c")
            .err()
            .map(|e| e.to_string())
            .unwrap_or_default();
        assert!(message.contains("a.b.c"));
    }
}
