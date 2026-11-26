use std::fs::{self, File, OpenOptions};
use std::io::{self, Read};
use std::path::{Path, PathBuf};
use std::thread;
use std::time::{Duration, Instant};

use rusqlite::backup::{Backup, StepResult};
use rusqlite::{Connection as Raw, OpenFlags};
use thiserror::Error;
use tokio::sync::oneshot;

use crate::dsn::Target;
use crate::error::Error;

// The first 16 bytes of every SQLite database file.
const MAGIC: &[u8; 16] = b"SQLite format 3\0";
// The first bytes of the side files that SQLite would read back: a WAL file (either checksum byte order) and
// a hot rollback journal. A `-shm` file has no magic, but its header starts with the wal-index version
// 3007000 in the machine's byte order.
const WAL_MAGIC: [u32; 2] = [0x377f_0682, 0x377f_0683];
const JOURNAL_MAGIC: [u8; 8] = [0xd9, 0xd5, 0x05, 0xf9, 0x20, 0xa1, 0x63, 0xd7];
const SHM_VERSION: u32 = 3_007_000;
// What follows the database's name on the files SQLite keeps beside it; keep in step with `SIDECARS` in `_scratch.py`
const SIDE_FILES: [&str; 3] = ["-wal", "-shm", "-journal"];

// the pause before a busy or locked template is tried again
const BACKUP_PAUSE: Duration = Duration::from_millis(10);
// the wall-clock time a clone waits for a lock on the template before it gives up, matching rusqlite's default busy timeout; it bounds only that wait, never the copy itself
const BACKUP_BUDGET: Duration = Duration::from_secs(5);
// the Win32 `ERROR_SHARING_VIOLATION` and `ERROR_LOCK_VIOLATION`
const SHARING_VIOLATION: i32 = 32;
const LOCK_VIOLATION: i32 = 33;

#[derive(Debug, Error)]
pub enum AdminError {
    #[error("{0}")]
    InvalidName(String),
    #[error("the database {0:?} already exists")]
    Exists(String),
    #[error("the template database {0:?} does not exist")]
    TemplateMissing(String),
    #[error("{0:?} is not a SQLite database, so it is not touched")]
    NotSqlite(String),
    #[error("the database {0:?} has open connections; close them before removing the file")]
    InUse(String),
    #[error(
        "{0:?} is a symbolic link, so it is not touched: SQLite keeps the side files beside the file it \
         points to; name that file instead"
    )]
    Symlink(String),
    #[error("the directory {0:?} does not exist")]
    NoDirectory(String),
    #[error("the template database {0:?} is locked by another connection")]
    TemplateBusy(String),
    #[error("{context}: {source}")]
    Io {
        context: String,
        #[source]
        source: io::Error,
    },
    #[error(transparent)]
    Sqlite(#[from] Error),
}

impl From<rusqlite::Error> for AdminError {
    fn from(err: rusqlite::Error) -> Self {
        Self::Sqlite(err.into())
    }
}

/// A database as `list` reports it. A file has no comment and no sessions to count.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DatabaseInfo {
    pub name: String,
    pub comment: Option<String>,
    pub sessions: i64,
}

fn io_error(context: impl Into<String>, source: io::Error) -> AdminError {
    AdminError::Io {
        context: context.into(),
        source,
    }
}

fn check_name(name: &str) -> Result<&Path, AdminError> {
    if name.is_empty() {
        return Err(AdminError::InvalidName(
            "a database name must not be empty".to_owned(),
        ));
    }
    if name.contains('\0') {
        return Err(AdminError::InvalidName(
            "a database name must not contain a NUL byte".to_owned(),
        ));
    }
    Ok(Path::new(name))
}

// the directory a path lives in; a bare file name lives in the working directory
fn directory_of(path: &Path) -> &Path {
    match path.parent() {
        Some(parent) if !parent.as_os_str().is_empty() => parent,
        _ => Path::new("."),
    }
}

fn side_path(path: &Path, suffix: &str) -> PathBuf {
    let mut name = path.as_os_str().to_owned();
    name.push(suffix);
    PathBuf::from(name)
}

// SQLite never parses the name as a URI; see `Target::open_path`
fn open(path: &Path, flags: OpenFlags) -> Result<Raw, AdminError> {
    let name = Target::File(path.to_path_buf());
    Ok(Raw::open_with_flags(name.open_path(), flags)?)
}

// whether removing a file failed because another process (or connection) still holds it open
const fn sharing_violation(windows: bool, code: Option<i32>) -> bool {
    windows && matches!(code, Some(SHARING_VIOLATION | LOCK_VIOLATION))
}

// runs the blocking work on a thread of its own, as `Connection::connect` does, so it never blocks tokio
async fn on_thread<T, F>(job: F) -> Result<T, AdminError>
where
    T: Send + 'static,
    F: FnOnce() -> Result<T, AdminError> + Send + 'static,
{
    let (reply, answer) = oneshot::channel();
    thread::Builder::new()
        .name("rowx-sqlite-admin".to_owned())
        .spawn(move || {
            // the receiver is gone when the caller was cancelled, and then nobody wants the answer
            let _ = reply.send(job());
        })
        .map_err(|err| io_error("could not start the SQLite admin thread", err))?;
    answer.await.map_err(|_| {
        AdminError::Sqlite(Error::Interface(
            "the SQLite admin thread ended before it answered".to_owned(),
        ))
    })?
}

// whether an error says the path leads nowhere: no entry, or a parent that is not a directory
fn is_gone(err: &io::Error) -> bool {
    matches!(
        err.kind(),
        io::ErrorKind::NotFound | io::ErrorKind::NotADirectory
    )
}

// what is at `path`; `None` when nothing is, which includes a parent that is not a directory
fn stat(path: &Path) -> Result<Option<fs::Metadata>, AdminError> {
    match fs::metadata(path) {
        Ok(found) => Ok(Some(found)),
        Err(err) if is_gone(&err) => Ok(None),
        Err(err) => Err(io_error(
            format!("could not look at {}", path.display()),
            err,
        )),
    }
}

// what is at `path` itself, a symbolic link not followed
fn lstat(path: &Path) -> Result<Option<fs::Metadata>, AdminError> {
    match fs::symlink_metadata(path) {
        Ok(found) => Ok(Some(found)),
        Err(err) if is_gone(&err) => Ok(None),
        Err(err) => Err(io_error(
            format!("could not look at {}", path.display()),
            err,
        )),
    }
}

fn exists_now(path: &Path) -> Result<bool, AdminError> {
    Ok(stat(path)?.is_some_and(|found| found.is_file()))
}

/// Whether a regular file is at `name`.
///
/// # Errors
/// `InvalidName` for an empty name or one with a NUL byte, `Io` when the path cannot be inspected.
pub async fn exists(name: &str) -> Result<bool, AdminError> {
    let path = check_name(name)?.to_path_buf();
    on_thread(move || exists_now(&path)).await
}

fn create_empty(path: &Path) -> Result<File, AdminError> {
    // `create_new` fails when the name is taken, so an existing file is never overwritten
    OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
        .map_err(|err| match err.kind() {
            io::ErrorKind::AlreadyExists => AdminError::Exists(path.display().to_string()),
            io::ErrorKind::NotFound => {
                AdminError::NoDirectory(directory_of(path).display().to_string())
            }
            _ => io_error(format!("could not create {}", path.display()), err),
        })
}

fn remove_files(path: &Path) {
    for suffix in std::iter::once("").chain(SIDE_FILES) {
        let _ = fs::remove_file(side_path(path, suffix));
    }
}

fn clone_into(template: &Path, path: &Path, budget: Duration) -> Result<(), AdminError> {
    if !exists_now(template)? {
        return Err(AdminError::TemplateMissing(template.display().to_string()));
    }
    let source = open(template, OpenFlags::SQLITE_OPEN_READ_ONLY)?;
    // the claim comes before the copy, so a name that is taken stops here
    drop(create_empty(path)?);
    let copied = copy_pages(&source, path, template, budget);
    if copied.is_err() {
        // the file is ours: the claim above succeeded, so nothing else wrote it
        remove_files(path);
    }
    copied
}

// the backup API reads a consistent snapshot, side files included, and never renumbers rowids as `VACUUM INTO` can
fn copy_pages(
    source: &Raw,
    path: &Path,
    template: &Path,
    budget: Duration,
) -> Result<(), AdminError> {
    // the source's own busy handler would wait out its default timeout inside each step; the loop below owns the wait
    source.busy_timeout(Duration::ZERO)?;
    let mut target = open(path, OpenFlags::SQLITE_OPEN_READ_WRITE)?;
    let backup = Backup::new(source, &mut target)?;
    // `run_to_completion` retries a busy or locked source without pausing or ending, so the steps are driven here
    let deadline = Instant::now() + budget;
    loop {
        // -1 copies every page in one step under the source's read lock, so no writer can restart the backup; only getting that lock can wait
        match backup.step(-1)? {
            StepResult::Done => return Ok(()),
            StepResult::Busy | StepResult::Locked => {
                if Instant::now() >= deadline {
                    return Err(AdminError::TemplateBusy(template.display().to_string()));
                }
                thread::sleep(BACKUP_PAUSE);
            }
            // `More` cannot happen when every page is copied in one step, and `StepResult` is non-exhaustive; a result this code does not expect is an error, not a reason to loop
            _ => {
                return Err(AdminError::Sqlite(Error::Interface(
                    "the backup ended with a result this driver does not know".to_owned(),
                )));
            }
        }
    }
}

/// Creates the database file at `name`, empty or as a copy of `template`.
///
/// A copy goes through SQLite's backup API: a file copy misses the side files and can catch a write
/// half done, and `VACUUM INTO` may renumber the rowids of a table that has no `INTEGER PRIMARY KEY`.
///
/// # Errors
/// `Exists` when the name is taken (nothing is overwritten), `TemplateMissing`, `TemplateBusy` when the template
/// stays locked by another connection for longer than the budget, `NoDirectory` when the
/// parent directory does not exist, `Sqlite` when the template cannot be read, `Io` otherwise. A copy that
/// fails leaves no file behind.
pub async fn create(name: &str, template: Option<&str>) -> Result<(), AdminError> {
    let path = check_name(name)?.to_path_buf();
    let template = template.map(check_name).transpose()?.map(Path::to_path_buf);
    on_thread(move || {
        template.map_or_else(
            || create_wal(&path),
            |template| clone_into(&template, &path, BACKUP_BUDGET),
        )
    })
    .await
}

// The switch writes the header, so the file is a database `drop` recognises, and it is already in the mode
// every read-write connection asks for, so connections that open it at once never race to switch it.
fn create_wal(path: &Path) -> Result<(), AdminError> {
    drop(create_empty(path)?);
    let switched = open(path, OpenFlags::SQLITE_OPEN_READ_WRITE).and_then(|raw| {
        let mode: String =
            raw.pragma_update_and_check(None, "journal_mode", "WAL", |row| row.get(0))?;
        Ok(mode)
    });
    match switched {
        Ok(mode) if mode.eq_ignore_ascii_case("wal") => Ok(()),
        Ok(mode) => {
            remove_files(path);
            Err(AdminError::Sqlite(Error::from_code(
                rusqlite::ffi::SQLITE_ERROR,
                format!("could not switch the new database to WAL; it stayed in {mode} mode"),
                None,
            )))
        }
        Err(err) => {
            // the file is ours: the claim above succeeded
            remove_files(path);
            Err(err)
        }
    }
}

// the first `N` bytes, or `None` for a shorter file
fn head<const N: usize>(path: &Path) -> Result<Option<[u8; N]>, AdminError> {
    let mut file = File::open(path)
        .map_err(|err| io_error(format!("could not open {}", path.display()), err))?;
    let mut head = [0_u8; N];
    let mut filled = 0;
    while filled < N {
        let read = file
            .read(&mut head[filled..])
            .map_err(|err| io_error(format!("could not read {}", path.display()), err))?;
        if read == 0 {
            return Ok(None);
        }
        filled += read;
    }
    Ok(Some(head))
}

// Only the header proves a file is a database. An empty file opens as an empty database, but so would any
// empty file a mistyped URL names, and `create` writes the header, so an empty file is not one.
fn is_sqlite_file(path: &Path) -> Result<bool, AdminError> {
    Ok(head::<16>(path)?.is_some_and(|found| found == *MAGIC))
}

// Whether an orphaned side file holds what SQLite would read back into a database created at the same path
fn is_side_file(path: &Path, suffix: &str) -> Result<bool, AdminError> {
    Ok(match suffix {
        "-wal" => {
            head::<4>(path)?.is_some_and(|found| WAL_MAGIC.contains(&u32::from_be_bytes(found)))
        }
        "-journal" => head::<8>(path)?.is_some_and(|found| found == JOURNAL_MAGIC),
        "-shm" => head::<4>(path)?.is_some_and(|found| {
            u32::from_le_bytes(found) == SHM_VERSION || u32::from_be_bytes(found) == SHM_VERSION
        }),
        _ => false,
    })
}

fn remove_one(path: &Path, windows: bool) -> Result<(), AdminError> {
    match fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(err) if is_gone(&err) => Ok(()),
        Err(err) if sharing_violation(windows, err.raw_os_error()) => {
            Err(AdminError::InUse(path.display().to_string()))
        }
        Err(err) => Err(io_error(
            format!("could not remove {}", path.display()),
            err,
        )),
    }
}

// beside a database, SQLite itself uses (and overwrites) these names, so they are its own
fn remove_side_files(path: &Path) -> Result<(), AdminError> {
    for suffix in SIDE_FILES {
        remove_one(&side_path(path, suffix), cfg!(windows))?;
    }
    Ok(())
}

// With no database to vouch for them, only side files whose content shows SQLite wrote them are removed
fn remove_orphans(path: &Path) -> Result<(), AdminError> {
    for suffix in SIDE_FILES {
        let side = side_path(path, suffix);
        let regular = lstat(&side)?.is_some_and(|found| found.file_type().is_file());
        if regular && is_side_file(&side, suffix)? {
            remove_one(&side, cfg!(windows))?;
        }
    }
    Ok(())
}

// Every connection to a WAL database holds a shared lock on the file for as long as it is open, so an
// exclusive lock taken without waiting shows nobody else has it open. In a rollback mode an idle connection
// holds no lock, and a connection that opens after this check is not seen: the guard is best effort. Any
// failure other than a lock held elsewhere leaves the decision to the removal.
fn ensure_unused(path: &Path) -> Result<(), AdminError> {
    let probe = || -> Result<(), AdminError> {
        let raw = open(path, OpenFlags::SQLITE_OPEN_READ_WRITE)?;
        raw.busy_timeout(Duration::ZERO)?;
        // in WAL mode only the exclusive locking mode takes the file's exclusive lock for a write transaction
        raw.pragma_update(None, "locking_mode", "EXCLUSIVE")?;
        raw.execute_batch("BEGIN EXCLUSIVE; ROLLBACK;")?;
        Ok(())
    };
    match probe() {
        Err(AdminError::Sqlite(err)) if err.is_busy() => {
            Err(AdminError::InUse(path.display().to_string()))
        }
        _ => Ok(()),
    }
}

fn drop_now(path: &Path, force: bool) -> Result<(), AdminError> {
    let Some(found) = lstat(path)? else {
        // orphans of a deleted file stay out of the way of a later `create`, which would replay them
        return remove_orphans(path);
    };
    // removing a link would leave the database it points to, and its side files, where they are
    if found.file_type().is_symlink() {
        return Err(AdminError::Symlink(path.display().to_string()));
    }
    if !found.is_file() {
        return Err(AdminError::NotSqlite(path.display().to_string()));
    }
    // a mistyped URL must never delete a file that is not a database
    if !is_sqlite_file(path)? {
        return Err(AdminError::NotSqlite(path.display().to_string()));
    }
    // Windows refuses to remove an open file on its own; elsewhere the writers would carry on into a file
    // nobody can open again
    if !force && !cfg!(windows) {
        ensure_unused(path)?;
    }
    // the main file goes first: if it is held open nothing else is removed
    remove_one(path, cfg!(windows))?;
    remove_side_files(path)
}

/// Removes the database file and its `-wal`, `-shm` and `-journal` side files.
///
/// A missing file is not an error, but its orphaned side files are still removed when their content shows
/// SQLite wrote them: left behind, a later `create` at the same path would make SQLite replay a stale WAL or
/// hot journal into the new database. Off Windows, a database another connection holds open is refused
/// unless `force` is set (best effort; see `ensure_unused`).
///
/// # Errors
/// `NotSqlite` for a directory or a file that does not start with the SQLite header (an empty file
/// included), `Symlink` for a symbolic link (nothing is removed for either), `InUse` when the database is
/// open elsewhere, `Io` otherwise.
pub async fn drop_database(name: &str, force: bool) -> Result<(), AdminError> {
    let path = check_name(name)?.to_path_buf();
    on_thread(move || drop_now(&path, force)).await
}

fn list_now(anchor: &Path, prefix: &str) -> Result<Vec<DatabaseInfo>, AdminError> {
    let directory = directory_of(anchor);
    let entries = match fs::read_dir(directory) {
        Ok(entries) => entries,
        Err(err) if err.kind() == io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(err) => {
            return Err(io_error(
                format!("could not list {}", directory.display()),
                err,
            ));
        }
    };
    let mut found = Vec::new();
    for entry in entries.flatten() {
        let file_name = entry.file_name();
        let Some(base) = file_name.to_str() else {
            continue;
        };
        if !base.starts_with(prefix) || SIDE_FILES.iter().any(|suffix| base.ends_with(suffix)) {
            continue;
        }
        let path = entry.path();
        // a file that vanishes or cannot be read is not a database this call can vouch for
        if !matches!(exists_now(&path), Ok(true)) || !matches!(is_sqlite_file(&path), Ok(true)) {
            continue;
        }
        // the same form `with_database` accepts: the DSN's own directory, then the file name
        let name = anchor
            .parent()
            .map_or_else(|| PathBuf::from(base), |parent| parent.join(base));
        found.push(DatabaseInfo {
            name: name.to_string_lossy().into_owned(),
            comment: None,
            sessions: 0,
        });
    }
    found.sort_by(|a, b| a.name.cmp(&b.name));
    Ok(found)
}

/// The SQLite files beside `anchor` (the path the DSN names) whose file name starts with `prefix`, side
/// files left out. A directory that does not exist holds none.
///
/// # Errors
/// `InvalidName` for a bad anchor, `Io` when the directory cannot be read.
pub async fn list(anchor: &str, prefix: &str) -> Result<Vec<DatabaseInfo>, AdminError> {
    let anchor = check_name(anchor)?.to_path_buf();
    let prefix = prefix.to_owned();
    on_thread(move || list_now(&anchor, &prefix)).await
}

fn ready_now(path: &Path, seconds: f64) -> Result<(), AdminError> {
    let directory = directory_of(path);
    if !fs::metadata(directory).is_ok_and(|found| found.is_dir()) {
        return Err(AdminError::NoDirectory(directory.display().to_string()));
    }
    // a missing file is fine: `create` comes next
    match stat(path)? {
        None => return Ok(()),
        Some(found) if !found.is_file() => {
            return Err(AdminError::NotSqlite(path.display().to_string()));
        }
        Some(_) => {}
    }
    let connection = open(path, OpenFlags::SQLITE_OPEN_READ_ONLY)?;
    if seconds.is_finite() && seconds > 0.0 {
        connection.busy_timeout(Duration::from_secs_f64(seconds.min(f64::from(u32::MAX))))?;
    }
    // opening reads nothing, so this is the first read of the header: a file that is not a database fails here
    connection.query_row("SELECT count(*) FROM sqlite_master", [], |_| Ok(()))?;
    Ok(())
}

/// Checks that the database can be reached: its directory exists and, if the file does, it opens as a
/// SQLite database. `seconds` bounds how long a lock is waited for.
///
/// # Errors
/// `NoDirectory`, `NotSqlite` for something that is not a regular file (a directory), or `Sqlite`
/// (`SQLITE_NOTADB` for a file that is not a database).
pub async fn ready(name: &str, seconds: f64) -> Result<(), AdminError> {
    let path = check_name(name)?.to_path_buf();
    on_thread(move || ready_now(&path, seconds)).await
}

#[cfg(test)]
mod tests {
    use std::error::Error as StdError;
    use std::ffi::OsString;
    use std::fs;
    use std::path::Path;

    use rusqlite::{Connection as Raw, ffi};
    use tempfile::tempdir;

    use std::time::{Duration, Instant};

    use super::{
        AdminError, JOURNAL_MAGIC, MAGIC, SHM_VERSION, clone_into, create, drop_database, exists,
        list, ready, sharing_violation,
    };
    use crate::error::Error;

    type Outcome = Result<(), Box<dyn StdError>>;

    fn path_str(path: &Path) -> String {
        path.to_string_lossy().into_owned()
    }

    fn database(path: &Path) -> Result<Raw, rusqlite::Error> {
        let connection = Raw::open(path)?;
        connection
            .execute_batch("CREATE TABLE t (v TEXT); INSERT INTO t VALUES ('a'), ('b'), ('c');")?;
        Ok(connection)
    }

    fn rows(path: &Path) -> Result<Vec<(i64, String)>, rusqlite::Error> {
        let raw = Raw::open(path)?;
        let mut statement = raw.prepare("SELECT rowid, v FROM t ORDER BY rowid")?;
        statement
            .query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?
            .collect()
    }

    #[tokio::test]
    async fn create_makes_an_empty_database_and_never_overwrites() -> Outcome {
        let dir = tempdir()?;
        let name = path_str(&dir.path().join("a.db"));
        assert!(!exists(&name).await?);
        create(&name, None).await?;
        assert!(exists(&name).await?);
        assert!(matches!(
            create(&name, None).await,
            Err(AdminError::Exists(_))
        ));
        // an empty database with the header written, already in WAL mode
        let raw = Raw::open(&name)?;
        let tables: i64 = raw.query_row("SELECT count(*) FROM sqlite_master", [], |r| r.get(0))?;
        assert_eq!(tables, 0);
        let mode: String = raw.query_row("PRAGMA journal_mode", [], |r| r.get(0))?;
        assert_eq!(mode, "wal");
        assert_eq!(fs::read(&name)?.get(..16), Some(MAGIC.as_slice()));
        Ok(())
    }

    #[tokio::test]
    async fn create_in_a_missing_directory_is_a_clear_error() -> Outcome {
        let dir = tempdir()?;
        let name = path_str(&dir.path().join("nope").join("a.db"));
        assert!(matches!(
            create(&name, None).await,
            Err(AdminError::NoDirectory(_))
        ));
        assert!(!exists(&name).await?);
        Ok(())
    }

    #[tokio::test]
    async fn names_that_cannot_be_paths_are_rejected() {
        assert!(matches!(exists("").await, Err(AdminError::InvalidName(_))));
        assert!(matches!(
            create("a\0b", None).await,
            Err(AdminError::InvalidName(_))
        ));
    }

    #[tokio::test]
    async fn a_clone_keeps_rows_and_rowids_after_deletes() -> Outcome {
        let dir = tempdir()?;
        let template = dir.path().join("template.db");
        let source = database(&template)?;
        // no INTEGER PRIMARY KEY: deleting rowid 2 would let a VACUUM renumber 3 to 2
        source.execute("DELETE FROM t WHERE v = 'b'", [])?;
        drop(source);
        let copy = dir.path().join("copy.db");
        create(&path_str(&copy), Some(&path_str(&template))).await?;
        assert_eq!(rows(&copy)?, vec![(1, "a".to_owned()), (3, "c".to_owned())]);
        Ok(())
    }

    #[tokio::test]
    async fn a_clone_includes_what_only_the_wal_holds() -> Outcome {
        let dir = tempdir()?;
        let template = dir.path().join("template.db");
        let source = database(&template)?;
        source.execute_batch("PRAGMA journal_mode = WAL; INSERT INTO t VALUES ('d');")?;
        // the source stays open, so the `d` row is still in the `-wal` file
        let copy = dir.path().join("copy.db");
        create(&path_str(&copy), Some(&path_str(&template))).await?;
        assert_eq!(rows(&copy)?.len(), 4);
        drop(source);
        Ok(())
    }

    #[tokio::test]
    async fn a_clone_needs_a_template_and_a_free_name() -> Outcome {
        let dir = tempdir()?;
        let missing = path_str(&dir.path().join("missing.db"));
        let target = dir.path().join("t.db");
        assert!(matches!(
            create(&path_str(&target), Some(&missing)).await,
            Err(AdminError::TemplateMissing(_))
        ));
        assert!(!target.exists());
        let template = dir.path().join("template.db");
        drop(database(&template)?);
        fs::write(&target, b"mine")?;
        assert!(matches!(
            create(&path_str(&target), Some(&path_str(&template))).await,
            Err(AdminError::Exists(_))
        ));
        assert_eq!(fs::read(&target)?, b"mine");
        Ok(())
    }

    #[tokio::test]
    async fn a_failed_clone_leaves_no_file() -> Outcome {
        let dir = tempdir()?;
        let template = dir.path().join("junk.db");
        fs::write(&template, [7_u8; 4096])?;
        let target = dir.path().join("t.db");
        let err = create(&path_str(&target), Some(&path_str(&template))).await;
        assert!(
            matches!(&err, Err(AdminError::Sqlite(Error::Sqlite { code, .. })) if code & 0xff == ffi::SQLITE_NOTADB),
            "{err:?}"
        );
        assert!(!target.exists());
        Ok(())
    }

    #[tokio::test]
    async fn drop_removes_the_file_and_its_side_files() -> Outcome {
        let dir = tempdir()?;
        let path = dir.path().join("a.db");
        drop(database(&path)?);
        for suffix in ["-wal", "-shm", "-journal"] {
            fs::write(format!("{}{suffix}", path.display()), b"x")?;
        }
        fs::write(dir.path().join("a.db.bak"), b"x")?;
        drop_database(&path_str(&path), false).await?;
        let left = fs::read_dir(dir.path())?
            .map(|entry| entry.map(|e| e.file_name()))
            .collect::<Result<Vec<_>, _>>()?;
        assert_eq!(left, vec![OsString::from("a.db.bak")]);
        Ok(())
    }

    #[tokio::test]
    async fn dropping_a_missing_file_removes_its_orphaned_side_files() -> Outcome {
        let dir = tempdir()?;
        let path = dir.path().join("a.db");
        // no connection is open, so the files can go on every platform; each starts as SQLite writes it
        let mut wal = 0x377f_0682_u32.to_be_bytes().to_vec();
        wal.extend([0_u8; 28]);
        let mut journal = JOURNAL_MAGIC.to_vec();
        journal.extend([0_u8; 20]);
        let mut shm = SHM_VERSION.to_ne_bytes().to_vec();
        shm.extend([0_u8; 60]);
        for (suffix, content) in [("-wal", &wal), ("-journal", &journal), ("-shm", &shm)] {
            fs::write(format!("{}{suffix}", path.display()), content)?;
        }
        fs::write(dir.path().join("a.db.keep"), b"x")?;
        assert!(!path.exists());
        drop_database(&path_str(&path), false).await?;
        let left = fs::read_dir(dir.path())?
            .map(|entry| entry.map(|e| e.file_name()))
            .collect::<Result<Vec<_>, _>>()?;
        assert_eq!(left, vec![OsString::from("a.db.keep")]);
        create(&path_str(&path), None).await?;
        assert!(fs::metadata(&path)?.len() > 0);
        Ok(())
    }

    #[tokio::test]
    async fn dropping_a_missing_file_keeps_side_named_files_sqlite_did_not_write() -> Outcome {
        let dir = tempdir()?;
        let path = dir.path().join("cfg");
        for suffix in ["-wal", "-shm", "-journal"] {
            fs::write(
                format!("{}{suffix}", path.display()),
                b"important user data",
            )?;
        }
        // empty ones prove nothing either
        let empty = dir.path().join("other");
        for suffix in ["-wal", "-shm", "-journal"] {
            fs::write(format!("{}{suffix}", empty.display()), b"")?;
        }
        drop_database(&path_str(&path), false).await?;
        drop_database(&path_str(&empty), false).await?;
        assert_eq!(fs::read_dir(dir.path())?.count(), 6);
        Ok(())
    }

    #[tokio::test]
    async fn dropping_a_missing_file_is_not_an_error() -> Outcome {
        let dir = tempdir()?;
        drop_database(&path_str(&dir.path().join("none.db")), false).await?;
        drop_database(&path_str(&dir.path().join("nodir").join("none.db")), false).await?;
        Ok(())
    }

    #[tokio::test]
    async fn a_path_below_a_regular_file_is_missing_for_every_call() -> Outcome {
        let dir = tempdir()?;
        let plain = dir.path().join("plain");
        fs::write(&plain, b"x")?;
        let name = path_str(&plain.join("x.db"));
        assert!(!exists(&name).await?);
        drop_database(&name, false).await?;
        assert!(!exists(&name).await?);
        assert_eq!(fs::read(&plain)?, b"x");
        Ok(())
    }

    #[tokio::test]
    async fn drop_refuses_what_is_not_a_database() -> Outcome {
        let dir = tempdir()?;
        let text = dir.path().join("notes.txt");
        fs::write(&text, b"keep me, I am not a database")?;
        let short = dir.path().join("short");
        fs::write(&short, b"SQLite")?;
        for refused in [&text, &short, &dir.path().to_path_buf()] {
            assert!(
                matches!(
                    drop_database(&path_str(refused), false).await,
                    Err(AdminError::NotSqlite(_))
                ),
                "{refused:?}"
            );
        }
        assert!(text.exists() && short.exists());
        // an empty file opens as an empty database, but nothing shows it is one
        let empty = dir.path().join("__init__.py");
        fs::write(&empty, b"")?;
        assert!(matches!(
            drop_database(&path_str(&empty), false).await,
            Err(AdminError::NotSqlite(_))
        ));
        assert!(empty.exists());
        // what `create` makes, `drop` removes
        let made = dir.path().join("made.db");
        create(&path_str(&made), None).await?;
        drop_database(&path_str(&made), false).await?;
        assert!(!made.exists());
        let real = dir.path().join("real.db");
        drop(database(&real)?);
        assert_eq!(fs::read(&real)?.get(..16), Some(MAGIC.as_slice()));
        drop_database(&path_str(&real), false).await?;
        assert!(!real.exists());
        Ok(())
    }

    #[tokio::test]
    async fn list_names_the_databases_beside_the_anchor_without_side_files() -> Outcome {
        let dir = tempdir()?;
        drop(database(&dir.path().join("rowx_b.db"))?);
        drop(database(&dir.path().join("rowx_a.db"))?);
        fs::write(dir.path().join("rowx_empty.db"), b"")?;
        fs::write(dir.path().join("rowx_a.db-wal"), b"x")?;
        fs::write(dir.path().join("rowx_a.db-journal"), b"x")?;
        fs::write(dir.path().join("rowx_text.db"), b"not a database")?;
        fs::write(dir.path().join("other.db"), b"")?;
        fs::create_dir(dir.path().join("rowx_dir"))?;
        let anchor = path_str(&dir.path().join("app.db"));
        let found = list(&anchor, "rowx_").await?;
        let names: Vec<_> = found.iter().map(|info| info.name.clone()).collect();
        assert_eq!(
            names,
            vec![
                path_str(&dir.path().join("rowx_a.db")),
                path_str(&dir.path().join("rowx_b.db")),
            ]
        );
        assert!(found.iter().all(|i| i.comment.is_none() && i.sessions == 0));
        let none = list(&path_str(&dir.path().join("x").join("app.db")), "rowx_").await?;
        assert_eq!(none.len(), 0);
        Ok(())
    }

    #[tokio::test]
    async fn list_of_a_bare_name_looks_in_the_working_directory() -> Outcome {
        let found = list("app.db", "no_such_prefix_").await?;
        assert_eq!(found.len(), 0);
        Ok(())
    }

    #[tokio::test]
    async fn ready_checks_the_directory_and_an_existing_file() -> Outcome {
        let dir = tempdir()?;
        // a missing file is fine, `create` comes next
        ready(&path_str(&dir.path().join("new.db")), 1.0).await?;
        assert!(matches!(
            ready(&path_str(&dir.path().join("nope").join("a.db")), 1.0).await,
            Err(AdminError::NoDirectory(_))
        ));
        let good = dir.path().join("good.db");
        drop(database(&good)?);
        ready(&path_str(&good), 1.0).await?;
        let bad = dir.path().join("bad.db");
        fs::write(&bad, [9_u8; 4096])?;
        let err = ready(&path_str(&bad), 1.0).await;
        assert!(
            matches!(&err, Err(AdminError::Sqlite(Error::Sqlite { name, .. })) if *name == "SQLITE_NOTADB"),
            "{err:?}"
        );
        Ok(())
    }

    #[tokio::test]
    async fn ready_refuses_a_path_that_is_not_a_regular_file() -> Outcome {
        let dir = tempdir()?;
        let folder = dir.path().join("folder");
        fs::create_dir(&folder)?;
        assert!(matches!(
            ready(&path_str(&folder), 1.0).await,
            Err(AdminError::NotSqlite(_))
        ));
        Ok(())
    }

    #[test]
    fn a_template_that_stays_locked_ends_the_clone_and_leaves_no_file() -> Outcome {
        let dir = tempdir()?;
        let template = dir.path().join("template.db");
        let holder = Raw::open(&template)?;
        holder.execute_batch(
            "PRAGMA journal_mode = DELETE; CREATE TABLE t (v TEXT); INSERT INTO t VALUES ('a');",
        )?;
        // an exclusive lock with a write in flight, so a reader cannot take even a shared lock
        holder.execute_batch("BEGIN EXCLUSIVE; INSERT INTO t VALUES ('b');")?;
        let target = dir.path().join("copy.db");
        let started = Instant::now();
        let copied = clone_into(&template, &target, Duration::from_millis(50));
        assert!(
            matches!(copied, Err(AdminError::TemplateBusy(_))),
            "{copied:?}"
        );
        assert!(started.elapsed() < Duration::from_secs(4));
        for suffix in std::iter::once("").chain(super::SIDE_FILES) {
            assert!(!super::side_path(&target, suffix).exists(), "{suffix}");
        }
        holder.execute_batch("ROLLBACK")?;
        Ok(())
    }

    #[test]
    fn a_large_template_clones_even_with_a_zero_budget() -> Outcome {
        let dir = tempdir()?;
        let template = dir.path().join("big.db");
        let big = Raw::open(&template)?;
        big.execute_batch("CREATE TABLE t (v BLOB);")?;
        {
            let mut insert = big.prepare("INSERT INTO t VALUES (zeroblob(1024))")?;
            for _ in 0..4000 {
                insert.execute([])?;
            }
        }
        let pages: i64 = big.query_row("PRAGMA page_count", [], |row| row.get(0))?;
        assert!(pages > 256, "{pages}");
        drop(big);
        // the budget bounds only the wait for a lock, never the copy
        let target = dir.path().join("big_copy.db");
        clone_into(&template, &target, Duration::ZERO)?;
        let ids = |path: &Path| -> Result<Vec<i64>, rusqlite::Error> {
            let raw = Raw::open(path)?;
            let mut statement = raw.prepare("SELECT rowid FROM t ORDER BY rowid")?;
            statement.query_map([], |r| r.get(0))?.collect()
        };
        let copied = ids(&target)?;
        assert_eq!(copied.len(), 4000);
        assert_eq!(copied, ids(&template)?);
        Ok(())
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn drop_refuses_a_symlink_and_leaves_the_database_it_points_to() -> Outcome {
        let dir = tempdir()?;
        let real = dir.path().join("real.db");
        let holder = database(&real)?;
        holder.execute_batch("PRAGMA journal_mode = WAL; INSERT INTO t VALUES ('d');")?;
        drop(holder);
        let link = dir.path().join("link.db");
        std::os::unix::fs::symlink(&real, &link)?;
        assert!(matches!(
            drop_database(&path_str(&link), false).await,
            Err(AdminError::Symlink(_))
        ));
        assert!(link.is_symlink() && real.exists());
        assert_eq!(rows(&real)?.len(), 4);
        // a link to nothing is refused too, so it is never mistaken for a missing file
        let dangling = dir.path().join("dangling.db");
        std::os::unix::fs::symlink(dir.path().join("gone.db"), &dangling)?;
        assert!(matches!(
            drop_database(&path_str(&dangling), false).await,
            Err(AdminError::Symlink(_))
        ));
        Ok(())
    }

    #[cfg(not(windows))]
    #[tokio::test]
    async fn drop_refuses_a_database_another_connection_holds_open() -> Outcome {
        let dir = tempdir()?;
        let path = dir.path().join("used.db");
        create(&path_str(&path), None).await?;
        let holder = Raw::open(&path)?;
        holder.execute_batch("CREATE TABLE t (v TEXT); INSERT INTO t VALUES ('a');")?;
        assert!(matches!(
            drop_database(&path_str(&path), false).await,
            Err(AdminError::InUse(_))
        ));
        assert!(path.exists());
        assert_eq!(rows(&path)?.len(), 1);
        // a read-only connection holds it as well
        drop(holder);
        let reader = Raw::open_with_flags(&path, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)?;
        reader.query_row("SELECT count(*) FROM t", [], |r| r.get::<_, i64>(0))?;
        assert!(matches!(
            drop_database(&path_str(&path), false).await,
            Err(AdminError::InUse(_))
        ));
        // `force` removes it anyway, as before the check
        drop_database(&path_str(&path), true).await?;
        assert!(!path.exists());
        drop(reader);
        // once every connection is closed, the check passes
        let again = dir.path().join("again.db");
        create(&path_str(&again), None).await?;
        Raw::open(&again)?.execute_batch("CREATE TABLE t (v TEXT);")?;
        drop_database(&path_str(&again), false).await?;
        assert!(!again.exists());
        Ok(())
    }

    #[test]
    fn only_windows_reports_a_sharing_violation() {
        assert!(sharing_violation(true, Some(32)));
        assert!(sharing_violation(true, Some(33)));
        assert!(!sharing_violation(true, Some(5)));
        assert!(!sharing_violation(true, None));
        assert!(!sharing_violation(false, Some(32)));
    }
}
