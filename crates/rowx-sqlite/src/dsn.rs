use std::borrow::Cow;
use std::path::{Path, PathBuf};

use rusqlite::OpenFlags;
use thiserror::Error;

const HINT: &str =
    "use `sqlite:///relative.db`, `sqlite:////absolute/path.db` or `sqlite::memory:`";

// error text never echoes the input: the DSN is user text, and the hint is all the caller needs
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum DsnError {
    #[error("a SQLite DSN must start with `sqlite:`; {}", HINT)]
    MissingScheme,
    #[error("`file:` URIs are not supported; {}", HINT)]
    FileUri,
    #[error("a SQLite DSN needs `sqlite:///` before the path; {}", HINT)]
    MissingSlashes,
    #[error("a SQLite DSN has no host (`sqlite://host/...`); {}", HINT)]
    Host,
    #[error("the database path is empty; {}", HINT)]
    EmptyPath,
    #[error("`:memory:` is only accepted as `sqlite::memory:`; {}", HINT)]
    MemoryPath,
    #[error("the DSN option `{0}` is not supported; {hint}", hint = HINT)]
    UnsupportedOption(&'static str),
    #[error(
        "a SQLite DSN takes no query parameters (encode a literal `?` as `%3F`); {}",
        HINT
    )]
    Query,
    #[error(
        "a SQLite DSN takes no fragment (encode a literal `#` as `%23`); {}",
        HINT
    )]
    Fragment,
    #[error("the database path has an invalid percent escape; {}", HINT)]
    PercentEscape,
    #[error(
        "the database path is not valid UTF-8 after percent decoding; {}",
        HINT
    )]
    NotUtf8,
    #[error("the database path contains a NUL byte; {}", HINT)]
    Nul,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Target {
    Memory,
    File(PathBuf),
}

impl Target {
    /// The name handed to `sqlite3_open_v2`, which never parses it as a URI.
    ///
    /// Omitting `SQLITE_OPEN_URI` is not enough: the bundled build compiles with `SQLITE_USE_URI`, which
    /// is global and makes SQLite parse any name starting with `file:` as a URI. A path that decodes to
    /// `file:...` (for example from `%3A` escapes) would therefore smuggle in `?mode=memory` and similar
    /// options. Such a path is necessarily relative, so a `./` prefix keeps it a plain file name.
    #[must_use]
    pub fn open_path(&self) -> Cow<'_, Path> {
        match self {
            Self::Memory => Cow::Borrowed(Path::new(":memory:")),
            Self::File(path) => {
                let looks_like_uri = path
                    .as_os_str()
                    .as_encoded_bytes()
                    .get(..5)
                    .is_some_and(|head| head.eq_ignore_ascii_case(b"file:"));
                if looks_like_uri {
                    Cow::Owned(Path::new(".").join(path))
                } else {
                    Cow::Borrowed(path)
                }
            }
        }
    }
}

/// How the connection is opened; the caller picks it, the DSN never does.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OpenMode {
    /// The checker.
    ReadOnly,
    /// The runtime default: the file must already exist.
    ReadWrite,
    /// Opt-in: create the file when it is missing.
    ReadWriteCreate,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OpenOptions {
    pub target: Target,
    pub mode: OpenMode,
}

impl OpenOptions {
    #[must_use]
    pub const fn new(target: Target, mode: OpenMode) -> Self {
        Self { target, mode }
    }

    /// Flags are built from scratch, so `SQLITE_OPEN_URI` is never set. That alone does not stop URI
    /// parsing, because the bundled build compiles with the global `SQLITE_USE_URI`; see
    /// [`Target::open_path`], whose `./` prefix is what prevents it.
    #[must_use]
    pub fn flags(&self) -> OpenFlags {
        let access = match (&self.target, self.mode) {
            // a read-only private in-memory database is unusable, so it is always created read-write
            (Target::Memory, _) | (_, OpenMode::ReadWriteCreate) => {
                OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_CREATE
            }
            (_, OpenMode::ReadOnly) => OpenFlags::SQLITE_OPEN_READ_ONLY,
            (_, OpenMode::ReadWrite) => OpenFlags::SQLITE_OPEN_READ_WRITE,
        };
        // one worker thread owns each connection
        access | OpenFlags::SQLITE_OPEN_NO_MUTEX
    }
}

fn strip_scheme<'a>(dsn: &'a str, scheme: &str) -> Option<&'a str> {
    let head = dsn.get(..scheme.len())?;
    if head.eq_ignore_ascii_case(scheme) {
        dsn.get(scheme.len()..)
    } else {
        None
    }
}

/// Parses `sqlite:///relative.db`, `sqlite:////absolute/path.db` or `sqlite::memory:`.
///
/// Exactly one leading `/` is stripped from the path after `sqlite://`, so `sqlite:///C:/x.db` is the
/// drive path `C:/x.db` and `sqlite://///server/share/x.db` is the UNC path `//server/share/x.db`.
///
/// # Errors
/// A [`DsnError`] for any other form, including every query parameter and fragment.
pub fn parse(dsn: &str) -> Result<Target, DsnError> {
    let Some(rest) = strip_scheme(dsn, "sqlite:") else {
        return Err(if strip_scheme(dsn, "file:").is_some() {
            DsnError::FileUri
        } else {
            DsnError::MissingScheme
        });
    };
    if rest.contains('#') {
        return Err(DsnError::Fragment);
    }
    let (body, query) = match rest.split_once('?') {
        Some((body, query)) => (body, Some(query)),
        None => (rest, None),
    };
    if let Some(query) = query {
        return Err(query_error(query));
    }
    if body == ":memory:" {
        return Ok(Target::Memory);
    }
    let Some(authority_and_path) = body.strip_prefix("//") else {
        return Err(DsnError::MissingSlashes);
    };
    let (authority, path) = authority_and_path
        .split_once('/')
        .unwrap_or((authority_and_path, ""));
    if !authority.is_empty() {
        return Err(DsnError::Host);
    }
    if path.is_empty() {
        return Err(DsnError::EmptyPath);
    }
    let decoded = percent_decode(path)?;
    if decoded == ":memory:" {
        return Err(DsnError::MemoryPath);
    }
    Ok(Target::File(PathBuf::from(decoded)))
}

fn query_error(query: &str) -> DsnError {
    const KNOWN: [&str; 4] = ["mode", "immutable", "vfs", "cache"];
    for pair in query.split('&') {
        let key = pair.split_once('=').map_or(pair, |(key, _)| key);
        if let Some(known) = KNOWN.iter().find(|name| key.eq_ignore_ascii_case(name)) {
            return DsnError::UnsupportedOption(known);
        }
    }
    DsnError::Query
}

fn percent_decode(text: &str) -> Result<String, DsnError> {
    let mut out = Vec::with_capacity(text.len());
    let mut bytes = text.bytes();
    while let Some(byte) = bytes.next() {
        if byte != b'%' {
            out.push(byte);
            continue;
        }
        let high = bytes.next().and_then(hex_value);
        let low = bytes.next().and_then(hex_value);
        let (Some(high), Some(low)) = (high, low) else {
            return Err(DsnError::PercentEscape);
        };
        out.push(high << 4 | low);
    }
    let decoded = String::from_utf8(out).map_err(|_| DsnError::NotUtf8)?;
    if decoded.contains('\0') {
        return Err(DsnError::Nul);
    }
    Ok(decoded)
}

const fn hex_value(byte: u8) -> Option<u8> {
    match byte {
        b'0'..=b'9' => Some(byte - b'0'),
        b'a'..=b'f' => Some(byte - b'a' + 10),
        b'A'..=b'F' => Some(byte - b'A' + 10),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::{DsnError, OpenMode, OpenOptions, Target, parse};
    use rusqlite::OpenFlags;

    fn file(path: &str) -> Target {
        Target::File(path.into())
    }

    #[test]
    fn accepted_forms_parse_to_targets() {
        let cases = [
            ("sqlite:///relative.db", Ok(file("relative.db"))),
            ("sqlite:///dir/sub/rel.db", Ok(file("dir/sub/rel.db"))),
            ("sqlite:////abs/path.db", Ok(file("/abs/path.db"))),
            ("sqlite::memory:", Ok(Target::Memory)),
            ("SQLITE:///upper.db", Ok(file("upper.db"))),
            ("sqlite:///my%20file.db", Ok(file("my file.db"))),
            ("sqlite:///a%2Fb.db", Ok(file("a/b.db"))),
            ("sqlite:///caf%C3%A9.db", Ok(file("café.db"))),
            ("sqlite:///100%25.db", Ok(file("100%.db"))),
            ("sqlite:///what%3F.db", Ok(file("what?.db"))),
            ("sqlite:///tag%23.db", Ok(file("tag#.db"))),
            ("sqlite:///C:/x.db", Ok(file("C:/x.db"))),
            ("sqlite:///c:/dir/x.db", Ok(file("c:/dir/x.db"))),
            (
                "sqlite://///server/share/x.db",
                Ok(file("//server/share/x.db")),
            ),
        ];
        for (dsn, expected) in cases {
            assert_eq!(parse(dsn), expected, "{dsn}");
        }
    }

    #[test]
    fn rejected_forms_name_the_supported_form() {
        let cases = [
            ("sqlite:app.db", DsnError::MissingSlashes),
            ("sqlite:/app.db", DsnError::MissingSlashes),
            ("sqlite:", DsnError::MissingSlashes),
            ("app.db", DsnError::MissingScheme),
            ("/abs/app.db", DsnError::MissingScheme),
            ("", DsnError::MissingScheme),
            ("postgres:///app.db", DsnError::MissingScheme),
            ("sqlite://host/app.db", DsnError::Host),
            ("sqlite://host", DsnError::Host),
            ("sqlite://localhost/app.db", DsnError::Host),
            ("sqlite://:memory:", DsnError::Host),
            ("file:app.db", DsnError::FileUri),
            ("file:///app.db", DsnError::FileUri),
            ("FILE:app.db?mode=ro", DsnError::FileUri),
            ("sqlite:///", DsnError::EmptyPath),
            ("sqlite://", DsnError::EmptyPath),
            ("sqlite:///:memory:", DsnError::MemoryPath),
            ("sqlite:///%3Amemory%3A", DsnError::MemoryPath),
            (
                "sqlite:///a.db?mode=memory",
                DsnError::UnsupportedOption("mode"),
            ),
            (
                "sqlite:///a.db?immutable=1",
                DsnError::UnsupportedOption("immutable"),
            ),
            (
                "sqlite:///a.db?vfs=unix-none",
                DsnError::UnsupportedOption("vfs"),
            ),
            (
                "sqlite:///a.db?cache=shared",
                DsnError::UnsupportedOption("cache"),
            ),
            (
                "sqlite::memory:?cache=shared",
                DsnError::UnsupportedOption("cache"),
            ),
            (
                "sqlite:///a.db?x=1&MODE=ro",
                DsnError::UnsupportedOption("mode"),
            ),
            ("sqlite:///a.db?timeout=5", DsnError::Query),
            ("sqlite:///a.db?", DsnError::Query),
            ("sqlite:///a.db#frag", DsnError::Fragment),
            ("sqlite:///a.db?x=1#frag", DsnError::Fragment),
            ("sqlite:///bad%2.db", DsnError::PercentEscape),
            ("sqlite:///bad%zz.db", DsnError::PercentEscape),
            ("sqlite:///bad%", DsnError::PercentEscape),
            ("sqlite:///bad%C3.db", DsnError::NotUtf8),
            ("sqlite:///nul%00.db", DsnError::Nul),
        ];
        for (dsn, expected) in cases {
            let parsed = parse(dsn);
            let text = parsed.as_ref().err().map(ToString::to_string);
            assert_eq!(parsed, Err(expected), "{dsn}");
            assert!(
                text.is_some_and(|text| text.contains("sqlite:///")),
                "{dsn}"
            );
        }
    }

    #[test]
    fn error_text_never_echoes_the_dsn() {
        let text = parse("sqlite:///secret-name.db?mode=memory")
            .err()
            .map(|err| err.to_string());
        assert!(text.is_some_and(|text| !text.contains("secret-name")));
    }

    #[test]
    fn open_path_is_the_plain_name() {
        assert_eq!(Target::Memory.open_path().to_str(), Some(":memory:"));
        assert_eq!(
            Target::File("a/b.db".into()).open_path().to_str(),
            Some("a/b.db")
        );
    }

    #[test]
    fn open_path_neutralises_file_prefixes() -> Result<(), Box<dyn std::error::Error>> {
        for (dsn, expected) in [
            (
                "sqlite:///file:a.db%3Fmode%3Dmemory",
                "./file:a.db?mode=memory",
            ),
            ("sqlite:///file:a.db", "./file:a.db"),
            ("sqlite:///FILE:a.db", "./FILE:a.db"),
            ("sqlite:///File:a.db", "./File:a.db"),
            ("sqlite:///profile:a.db", "profile:a.db"),
        ] {
            let target = parse(dsn)?;
            let expected = expected.replace('/', std::path::MAIN_SEPARATOR_STR);
            assert_eq!(
                target.open_path().to_str(),
                Some(expected.as_str()),
                "{dsn}"
            );
        }
        Ok(())
    }

    #[cfg(unix)]
    #[test]
    fn open_path_neutralises_non_utf8_file_prefixes() {
        use std::os::unix::ffi::OsStringExt;

        let raw = std::ffi::OsString::from_vec(b"file:\xff".to_vec());
        let open = Target::File(std::path::PathBuf::from(raw))
            .open_path()
            .into_owned();
        assert!(
            open.as_os_str()
                .as_encoded_bytes()
                .starts_with(b"./file:\xff")
        );
    }

    // the bundled SQLite treats a `file:` name as a URI everywhere, so the neutralised name, opened
    // relative to the test cwd, must create a literal file whose name contains `file:` and `?`
    #[cfg(not(windows))]
    #[test]
    fn neutralised_name_opens_a_literal_file() -> Result<(), Box<dyn std::error::Error>> {
        let name = format!("file:rowx-dsn-test-{}.db?mode=memory", std::process::id());
        // the fallible work runs first so the cleanup below happens even when it fails
        let result = (|| -> Result<(), Box<dyn std::error::Error>> {
            let target = parse(&format!(
                "sqlite:///file:rowx-dsn-test-{}.db%3Fmode%3Dmemory",
                std::process::id()
            ))?;
            let open = target.open_path().into_owned();
            assert_eq!(open, std::path::Path::new(".").join(&name));
            let flags = OpenOptions::new(target, OpenMode::ReadWriteCreate).flags();
            let conn = rusqlite::Connection::open_with_flags(&open, flags)?;
            conn.execute_batch("CREATE TABLE t (a)")?;
            assert!(std::path::Path::new(&name).exists());
            Ok(())
        })();
        for suffix in ["", "-wal", "-shm", "-journal"] {
            let _ = std::fs::remove_file(format!("{name}{suffix}"));
        }
        result
    }

    #[test]
    fn flags_follow_the_mode_and_never_enable_uri() {
        let file = Target::File("a.db".into());
        let no_mutex = OpenFlags::SQLITE_OPEN_NO_MUTEX;
        let cases = [
            (
                OpenMode::ReadOnly,
                OpenFlags::SQLITE_OPEN_READ_ONLY | no_mutex,
            ),
            (
                OpenMode::ReadWrite,
                OpenFlags::SQLITE_OPEN_READ_WRITE | no_mutex,
            ),
            (
                OpenMode::ReadWriteCreate,
                OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_CREATE | no_mutex,
            ),
        ];
        for (mode, expected) in cases {
            let flags = OpenOptions::new(file.clone(), mode).flags();
            assert_eq!(flags, expected, "{mode:?}");
            assert!(!flags.contains(OpenFlags::SQLITE_OPEN_URI));
        }
    }

    #[test]
    fn memory_is_always_created_read_write() {
        for mode in [
            OpenMode::ReadOnly,
            OpenMode::ReadWrite,
            OpenMode::ReadWriteCreate,
        ] {
            let flags = OpenOptions::new(Target::Memory, mode).flags();
            assert!(
                flags.contains(OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_CREATE)
            );
            assert!(!flags.contains(OpenFlags::SQLITE_OPEN_URI));
        }
    }
}
