// who owns what: the owner file of a persistent directory, and the pid-named private roots of an instance

use std::ffi::OsString;
use std::fs::{self, File};
use std::io::{self, Write as _};
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};

use rustix::fs::{FlockOperation, flock};
use rustix::io::Errno;
use rustix::process::{Pid, getuid, test_kill_process};

const OWNER_SUFFIX: &str = ".rowx-owner";
const ROOT_PREFIX: &str = "rxe-";

#[must_use]
pub fn root_prefix(pid: u32) -> String {
    format!("{ROOT_PREFIX}{pid}-")
}

// the owner pid of a private root named `rxe-<pid>-<random>`
#[must_use]
pub fn parse_root_pid(name: &str) -> Option<u32> {
    let rest = name.strip_prefix(ROOT_PREFIX)?;
    let (pid, suffix) = rest.split_once('-')?;
    if suffix.is_empty() || pid.is_empty() || !pid.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    pid.parse().ok()
}

// The engine's own lock error carries no pid, so a persistent directory's owner records itself in `.<name>.rowx-owner`
// beside the directory. It cannot live inside: the engine refuses a root with entries it does not know, which would
// make a file left by a killed owner block the next open.
#[must_use]
pub fn owner_path(directory: &Path) -> Option<PathBuf> {
    let name = directory.file_name()?;
    let mut file = OsString::from(".");
    file.push(name);
    file.push(OWNER_SUFFIX);
    Some(directory.parent()?.join(file))
}

/// Best effort: a directory whose parent is not writable simply has no recorded owner.
///
/// The file is created exclusively under a temporary name and renamed over the owner file, so a symlink planted at
/// either name is never followed.
pub fn write_owner(directory: &Path, pid: u32) {
    let Some(path) = owner_path(directory) else {
        return;
    };
    let mut temporary = path.clone().into_os_string();
    temporary.push(format!(".{pid}.tmp"));
    let temporary = PathBuf::from(temporary);
    let _ = fs::remove_file(&temporary);
    let written = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&temporary)
        .and_then(|mut file| file.write_all(format!("{pid}\n").as_bytes()));
    if written.is_err() || fs::rename(&temporary, &path).is_err() {
        let _ = fs::remove_file(&temporary);
    }
}

#[must_use]
pub fn read_owner(directory: &Path) -> Option<u32> {
    fs::read_to_string(owner_path(directory)?)
        .ok()?
        .trim()
        .parse()
        .ok()
}

pub fn remove_owner(directory: &Path) {
    if let Some(path) = owner_path(directory) {
        let _ = fs::remove_file(path);
    }
}

#[must_use]
pub fn process_alive(pid: u32) -> bool {
    let Some(pid) = i32::try_from(pid).ok().and_then(Pid::from_raw) else {
        return false;
    };
    match test_kill_process(pid) {
        Ok(()) => true,
        // the process exists but belongs to someone else
        Err(error) => error == Errno::PERM,
    }
}

/// The pid of the live process that owns `directory`, when the owner file names one.
#[must_use]
pub fn live_owner(directory: &Path) -> Option<u32> {
    read_owner(directory).filter(|pid| process_alive(*pid))
}

const LOCK_FILE: &str = ".lock";

/// An advisory lock on a file inside a private root, held for the life of the instance.
///
/// The kernel drops it when the process dies, so a lock that can be taken means no live process uses the root,
/// whatever pid namespace it is in.
#[derive(Debug)]
pub struct RootLock {
    _file: File,
}

/// # Errors
/// When the lock file cannot be created or another process holds the lock.
pub fn lock_root(root: &Path) -> io::Result<RootLock> {
    let file = File::create(root.join(LOCK_FILE))?;
    flock(&file, FlockOperation::NonBlockingLockExclusive)?;
    Ok(RootLock { _file: file })
}

// What claiming a root for removal holds: the locked file, or nothing for a root that has no lock file.
type Claim = Option<File>;

// Some(claim) when the root is free: it has no lock file, or this call could lock it. The lock stays held (inside
// the claim) while the caller removes the root.
fn claim_root(root: &Path) -> Option<Claim> {
    File::open(root.join(LOCK_FILE)).map_or(Some(None), |file| {
        flock(&file, FlockOperation::NonBlockingLockExclusive)
            .ok()
            .map(|()| Some(file))
    })
}

/// Removes the private roots under `base` whose owner pid is not alive and whose lock is free; returns the paths it
/// removed.
pub fn sweep_orphans(base: &Path, alive: &dyn Fn(u32) -> bool) -> Vec<PathBuf> {
    let Ok(entries) = fs::read_dir(base) else {
        return Vec::new();
    };
    let me = getuid().as_raw();
    let mut removed = Vec::new();
    for entry in entries.flatten() {
        let Some(pid) = entry.file_name().to_str().and_then(parse_root_pid) else {
            continue;
        };
        if alive(pid) {
            continue;
        }
        // another pid namespace sharing this directory can make a live owner look dead: its lock still shows
        let Some(_claim) = claim_root(&entry.path()) else {
            continue;
        };
        // only our own directories, and never through a symlink
        let Ok(metadata) = fs::symlink_metadata(entry.path()) else {
            continue;
        };
        if !metadata.is_dir() || metadata.uid() != me {
            continue;
        }
        if fs::remove_dir_all(entry.path()).is_ok() {
            removed.push(entry.path());
        }
    }
    removed
}

#[cfg(test)]
mod tests {
    use std::io;

    use super::*;

    #[test]
    fn root_names_carry_the_owner_pid() {
        assert_eq!(
            parse_root_pid(&format!("{}abc123", root_prefix(4242))),
            Some(4242)
        );
        assert_eq!(parse_root_pid("rxe-12-"), None);
        assert_eq!(parse_root_pid("rxe--abc"), None);
        assert_eq!(parse_root_pid("rxe-x1-abc"), None);
        assert_eq!(parse_root_pid("tmpabc"), None);
        assert_eq!(parse_root_pid("rowx-embedded-abc"), None);
    }

    #[test]
    fn the_owner_file_sits_beside_the_directory() {
        assert_eq!(
            owner_path(Path::new("/data/pg")),
            Some(PathBuf::from("/data/.pg.rowx-owner"))
        );
        assert_eq!(owner_path(Path::new("/")), None);
    }

    #[test]
    fn the_owner_file_round_trips_and_a_missing_one_is_unknown() -> io::Result<()> {
        let base = tempfile::tempdir()?;
        let dir = base.path().join("pg");
        assert_eq!(read_owner(&dir), None);
        write_owner(&dir, 777);
        assert_eq!(read_owner(&dir), Some(777));
        remove_owner(&dir);
        assert_eq!(read_owner(&dir), None);
        if let Some(path) = owner_path(&dir) {
            fs::write(path, "not a pid")?;
        }
        assert_eq!(read_owner(&dir), None);
        Ok(())
    }

    #[test]
    fn this_process_is_alive_and_an_impossible_pid_is_not() {
        assert!(process_alive(std::process::id()));
        assert!(!process_alive(u32::MAX));
    }

    #[test]
    fn a_dead_owner_pid_is_not_reported() -> io::Result<()> {
        let base = tempfile::tempdir()?;
        let dir = base.path().join("pg");
        write_owner(&dir, std::process::id());
        assert_eq!(live_owner(&dir), Some(std::process::id()));
        write_owner(&dir, i32::MAX as u32);
        assert_eq!(live_owner(&dir), None);
        Ok(())
    }

    #[test]
    fn a_locked_root_is_never_swept_even_when_its_pid_looks_dead() -> io::Result<()> {
        let base = tempfile::tempdir()?;
        let live = base.path().join("rxe-111-aaaaaa");
        let dead = base.path().join("rxe-112-bbbbbb");
        fs::create_dir_all(&live)?;
        fs::create_dir_all(&dead)?;
        let held = lock_root(&live)?;
        // a second lock on the same root is refused while the first is held
        assert!(lock_root(&live).is_err());
        let removed = sweep_orphans(base.path(), &|_| false);
        assert_eq!(removed, vec![dead]);
        assert!(live.exists());
        drop(held);
        assert_eq!(sweep_orphans(base.path(), &|_| false), vec![live]);
        Ok(())
    }

    #[test]
    fn the_owner_file_is_not_written_through_a_symlink() -> io::Result<()> {
        let base = tempfile::tempdir()?;
        let dir = base.path().join("pg");
        let victim = base.path().join("victim");
        fs::write(&victim, "precious")?;
        if let Some(path) = owner_path(&dir) {
            std::os::unix::fs::symlink(&victim, &path)?;
            write_owner(&dir, 5);
            assert_eq!(fs::read_to_string(&victim)?, "precious");
            assert_eq!(read_owner(&dir), Some(5));
        }
        Ok(())
    }

    #[test]
    fn the_sweep_removes_only_the_roots_of_dead_owners() -> io::Result<()> {
        let base = tempfile::tempdir()?;
        let dead = base.path().join("rxe-111-aaaaaa");
        let live = base.path().join("rxe-222-bbbbbb");
        let foreign = base.path().join("somebody-elses");
        for dir in [&dead, &live, &foreign] {
            fs::create_dir_all(dir.join("nested"))?;
            fs::write(dir.join("nested").join("file"), "x")?;
        }
        let removed = sweep_orphans(base.path(), &|pid| pid == 222);
        assert_eq!(removed, vec![dead.clone()]);
        assert!(!dead.exists());
        assert!(live.exists());
        assert!(foreign.exists());
        Ok(())
    }
}
