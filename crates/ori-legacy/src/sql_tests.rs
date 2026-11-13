//! Actual source files, SQLite and kernel locks. Linux's full-range OFD lock
//! emulates Darwin's flock/fcntl conflict; it does not validate Darwin's ABI.
use super::*;
use sha2::{Digest, Sha256};
use std::{
    os::unix::fs::PermissionsExt,
    process::Command,
    sync::atomic::{AtomicBool, AtomicUsize, Ordering},
};

#[derive(Default)]
struct Charges {
    bytes: AtomicUsize,
    calls: AtomicUsize,
    chunks: Mutex<Vec<u64>>,
}
impl LegacyIoBudget for Charges {
    fn account(&self, bytes: u64, _: &LegacyCancellation) -> Result<()> {
        assert!((1..=512 * 1024).contains(&bytes));
        self.bytes.fetch_add(bytes as usize, Ordering::SeqCst);
        self.calls.fetch_add(1, Ordering::SeqCst);
        self.chunks.lock().unwrap().push(bytes);
        Ok(())
    }
}

fn wal_source(directory: &Path) -> PathBuf {
    let seed = directory.join("seed.sqlite");
    let db = Connection::open(&seed).unwrap();
    db.execute_batch("CREATE TABLE panorama(id INTEGER PRIMARY KEY,recorded_at TEXT,latitude REAL,longitude REAL,altitude REAL,speed REAL,heading REAL,levels BLOB,start_hz REAL,stop_hz REAL,step_hz REAL,unit TEXT); CREATE TABLE panorama_meta(key TEXT,value TEXT); CREATE TABLE threshold_line(line_index INTEGER,frequencies BLOB,levels BLOB); CREATE TABLE signal_event(id INTEGER,panorama_id INTEGER,recorded_at TEXT,frequency_hz REAL,bandwidth_hz REAL,power_over_w REAL,level_db REAL); PRAGMA journal_mode=WAL; PRAGMA wal_autocheckpoint=0; INSERT INTO panorama(id,recorded_at,levels) VALUES(42,'WAL-only committed row',x'000000000000f03f');").unwrap();
    let source = directory.join("source-é-é-😀%?#.pan");
    for suffix in ["", "-wal", "-shm"] {
        fs::copy(append(&seed, suffix), append(&source, suffix)).unwrap();
    }
    db.close().unwrap();
    source
}
fn append(path: &Path, suffix: &str) -> PathBuf {
    let mut value = path.as_os_str().to_owned();
    value.push(suffix);
    value.into()
}
fn hashes(source: &Path) -> Vec<Vec<u8>> {
    ["", "-wal", "-shm", "-journal"]
        .into_iter()
        .filter(|suffix| append(source, suffix).is_file())
        .map(|suffix| Sha256::digest(fs::read(append(source, suffix)).unwrap()).to_vec())
        .collect()
}

#[test]
fn readonly_fallback_is_limited_to_permission_and_readonly_filesystem_errors() {
    for code in [libc::EACCES, libc::EPERM, libc::EROFS] {
        assert!(readonly_open_denied(&std::io::Error::from_raw_os_error(
            code
        )));
    }
    for code in [
        libc::ENOENT,
        libc::ENOTDIR,
        libc::ELOOP,
        libc::EIO,
        libc::EMFILE,
        libc::EINTR,
    ] {
        assert!(!readonly_open_denied(&std::io::Error::from_raw_os_error(
            code
        )));
    }
}

#[tokio::test]
async fn readonly_raw_journal_caps_are_enforced_before_any_copy_charge() {
    for combined in [false, true] {
        let directory = tempfile::tempdir().unwrap();
        let source = wal_source(directory.path());
        let cap = fs::metadata(&source).unwrap().len();
        if combined {
            let journal = File::create(append(&source, "-journal")).unwrap();
            journal.set_len(cap).unwrap();
        } else {
            let wal = OpenOptions::new()
                .write(true)
                .open(append(&source, "-wal"))
                .unwrap();
            wal.set_len(cap + 1).unwrap();
        }
        let owner = PanOwner::open_owned(source.clone(), whole_readonly_lease(&source), true)
            .await
            .unwrap();
        let mut options = LegacyOptions {
            scratch_directory: directory.path().join("scratch"),
            ..LegacyOptions::default()
        };
        options.limits.max_database_bytes = cap;
        let before = hashes(&source);
        let budget = Arc::new(Charges::default());
        let result = owner
            .snapshot(
                &options,
                None,
                false,
                LegacyCancellation::default(),
                Some(budget.clone()),
            )
            .await;
        let refused = matches!(result, Err(LegacyError::ResourceLimit(_)));
        drop(result);
        owner.close().await.unwrap();
        assert!(refused);
        assert_eq!(budget.calls.load(Ordering::SeqCst), 0);
        assert_eq!(Arc::strong_count(&budget), 1);
        assert!(
            fs::read_dir(&options.scratch_directory)
                .unwrap()
                .next()
                .is_none()
        );
        assert_eq!(hashes(&source), before);
    }
}

#[tokio::test]
async fn readonly_snapshot_refuses_symlink_journal_without_copying_its_target() {
    let directory = tempfile::tempdir().unwrap();
    let source = wal_source(directory.path());
    let owner = PanOwner::open_owned(source.clone(), whole_readonly_lease(&source), true)
        .await
        .unwrap();
    let foreign = directory.path().join("owned-foreign-bytes");
    fs::write(&foreign, b"do not copy this target").unwrap();
    std::os::unix::fs::symlink(&foreign, append(&source, "-journal")).unwrap();
    let before = hashes(&source);
    let options = LegacyOptions {
        scratch_directory: directory.path().join("scratch"),
        ..LegacyOptions::default()
    };
    let budget = Arc::new(Charges::default());
    let result = owner
        .snapshot(
            &options,
            None,
            false,
            LegacyCancellation::default(),
            Some(budget.clone()),
        )
        .await;
    let refused = matches!(result, Err(LegacyError::Request(_)));
    drop(result);
    owner.close().await.unwrap();
    assert!(refused);
    assert_eq!(budget.calls.load(Ordering::SeqCst), 0);
    assert_eq!(hashes(&source), before);
    assert_eq!(fs::read(&foreign).unwrap(), b"do not copy this target");
    assert!(
        fs::read_dir(&options.scratch_directory)
            .unwrap()
            .next()
            .is_none()
    );
}

#[tokio::test]
async fn readonly_hot_rollback_journal_keeps_its_real_recovery_error() {
    let directory = tempfile::tempdir().unwrap();
    let seed = wal_source(directory.path());
    let db = Connection::open(&seed).unwrap();
    db.execute_batch("PRAGMA journal_mode=DELETE; PRAGMA cache_size=1; BEGIN IMMEDIATE; UPDATE panorama SET levels=zeroblob(131072) WHERE id=42;").unwrap();
    let source = directory.path().join("hot-journal.pan");
    fs::copy(&seed, &source).unwrap();
    fs::copy(append(&seed, "-journal"), append(&source, "-journal")).unwrap();
    db.execute_batch("ROLLBACK").unwrap();
    db.close().unwrap();
    let oracle = Connection::open_with_flags(&source, OpenFlags::SQLITE_OPEN_READ_ONLY).unwrap();
    let expected = oracle
        .query_row("SELECT id FROM panorama", [], |row| row.get::<_, i64>(0))
        .unwrap_err();
    assert_eq!(
        expected.sqlite_error_code(),
        Some(rusqlite::ErrorCode::ReadOnly)
    );
    oracle.close().unwrap();
    let owner = PanOwner::open_owned(source.clone(), whole_readonly_lease(&source), true)
        .await
        .unwrap();
    let before = hashes(&source);
    let options = LegacyOptions {
        scratch_directory: directory.path().join("scratch"),
        ..LegacyOptions::default()
    };
    let result = owner
        .snapshot(&options, None, false, LegacyCancellation::default(), None)
        .await;
    let refused =
        matches!(result, Err(LegacyError::Sqlite(message)) if message.contains("readonly"));
    owner.close().await.unwrap();
    assert!(
        refused,
        "hot-journal recovery was silently skipped or rewritten"
    );
    assert_eq!(hashes(&source), before);
    assert!(
        fs::read_dir(&options.scratch_directory)
            .unwrap()
            .next()
            .is_none()
    );
}
fn whole_readonly_lease(path: &Path) -> Arc<File> {
    #[cfg(target_os = "linux")]
    let file = {
        use std::os::fd::AsRawFd;
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .open(path)
            .unwrap();
        let lock = libc::flock {
            l_type: libc::F_WRLCK as _,
            l_whence: libc::SEEK_SET as _,
            l_start: 0,
            l_len: 0,
            l_pid: 0,
        };
        // Actual host Linux OFD range lock, standing in for Darwin's unified
        // whole-inode flock/fcntl locking, without pretending to run Darwin.
        assert_eq!(
            unsafe { libc::fcntl(file.as_raw_fd(), libc::F_OFD_SETLK, &lock) },
            0
        );
        file
    };
    #[cfg(target_os = "macos")]
    let file = {
        let file = File::open(path).unwrap();
        file.try_lock().unwrap();
        file
    };
    for suffix in ["", "-wal", "-shm", "-journal"] {
        if append(path, suffix).exists() {
            fs::set_permissions(append(path, suffix), fs::Permissions::from_mode(0o444)).unwrap();
        }
    }
    Arc::new(file)
}

fn probe_byte_in_process(path: &Path, busy: bool) {
    let output = Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "sql::tests::independent_process_byte_probe",
            "--nocapture",
        ])
        .env("ORI_LEGACY_BYTE_PROBE", path)
        .env("ORI_LEGACY_BYTE_BUSY", if busy { "1" } else { "0" })
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
}

#[test]
fn independent_process_byte_probe() {
    let Some(path) = std::env::var_os("ORI_LEGACY_BYTE_PROBE") else {
        return;
    };
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .open(path)
        .unwrap();
    if std::env::var("ORI_LEGACY_BYTE_BUSY").unwrap() == "1" {
        assert!(matches!(lock_source_byte(&file), Err(LegacyError::Busy)));
    } else {
        lock_source_byte(&file).unwrap();
    }
}

#[tokio::test]
async fn byte_lease_preserves_sqlite_changes_and_alias_exclusion_until_last_owner() {
    let directory = tempfile::tempdir().unwrap();
    let source = wal_source(directory.path());
    let alias = directory.path().join("alias.pan");
    fs::hard_link(&source, &alias).unwrap();
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .open(&source)
        .unwrap();
    lock_source_byte(&file).unwrap();
    let owner = PanOwner::open_owned(source.clone(), Arc::new(file), false)
        .await
        .unwrap();
    let options = LegacyOptions {
        scratch_directory: directory.path().join("scratch"),
        ..LegacyOptions::default()
    };
    let snapshot = owner
        .snapshot(&options, None, false, LegacyCancellation::default(), None)
        .await
        .unwrap();
    probe_byte_in_process(&alias, true);
    drop(File::open(&alias).unwrap()); // An unrelated close must not unlock OFD ownership.
    let external = Connection::open(&source).unwrap();
    external
        .execute(
            "UPDATE panorama SET recorded_at='external committed row' WHERE id=42",
            [],
        )
        .unwrap();
    external.close().unwrap();
    assert!(matches!(
        owner.check(snapshot.data_version).await,
        Err(LegacyError::Stale)
    ));
    #[cfg(target_os = "macos")]
    assert!(File::open(&alias).unwrap().try_lock().is_err());
    let retained = owner.lease();
    owner.close().await.unwrap();
    drop(owner);
    probe_byte_in_process(&alias, true);
    drop(retained);
    probe_byte_in_process(&alias, false);
    drop(snapshot);
}

struct Denied;
impl LegacyIoBudget for Denied {
    fn account(&self, _: u64, _: &LegacyCancellation) -> Result<()> {
        Err(LegacyError::ResourceLimit(
            "Denied actual raw-copy chunk".into(),
        ))
    }
}

struct GrowWal {
    first: AtomicBool,
    file: Mutex<File>,
    bytes: usize,
}

struct DenyStageCleanup {
    scratch: PathBuf,
    paths: Mutex<Option<(PathBuf, PathBuf)>>,
}
impl LegacyIoBudget for DenyStageCleanup {
    fn account(&self, _: u64, _: &LegacyCancellation) -> Result<()> {
        let outer = fs::read_dir(&self.scratch)
            .unwrap()
            .next()
            .unwrap()
            .unwrap()
            .path();
        let raw = fs::read_dir(&outer)
            .unwrap()
            .next()
            .unwrap()
            .unwrap()
            .path();
        fs::set_permissions(&raw, fs::Permissions::from_mode(0o000)).unwrap();
        *self.paths.lock().unwrap() = Some((outer, raw));
        Err(LegacyError::ResourceLimit(
            "Stop after owned-stage permission denial".into(),
        ))
    }
}

#[tokio::test]
async fn unconfirmed_readonly_stage_removal_retains_budget_and_original_inode() {
    let directory = tempfile::tempdir().unwrap();
    let source = wal_source(directory.path());
    let owner = PanOwner::open_owned(source.clone(), whole_readonly_lease(&source), true)
        .await
        .unwrap();
    let options = LegacyOptions {
        scratch_directory: directory.path().join("scratch"),
        ..LegacyOptions::default()
    };
    let before = hashes(&source);
    let budget = Arc::new(DenyStageCleanup {
        scratch: options.scratch_directory.clone(),
        paths: Mutex::new(None),
    });
    let result = owner
        .snapshot(
            &options,
            None,
            false,
            LegacyCancellation::default(),
            Some(budget.clone()),
        )
        .await;
    let reservation_retained = Arc::strong_count(&budget) > 1;
    let closing = owner.close().await;
    drop(owner);
    // Restore/remove the test-owned filesystem fault before asserting it.
    if let Some((outer, raw)) = budget.paths.lock().unwrap().take()
        && outer.exists()
    {
        fs::set_permissions(raw, fs::Permissions::from_mode(0o700)).unwrap();
        fs::remove_dir_all(outer).unwrap();
    }
    fs::set_permissions(&source, fs::Permissions::from_mode(0o644)).unwrap();
    let probe = OpenOptions::new()
        .read(true)
        .write(true)
        .open(&source)
        .unwrap();
    let source_retained = matches!(lock_source_byte(&probe), Err(LegacyError::Busy));
    drop(probe);
    assert!(matches!(result, Err(LegacyError::CleanupPending)));
    assert!(
        matches!(closing, Err(LegacyError::CleanupPending)),
        "SQL close acknowledged unconfirmed private cleanup"
    );
    assert!(
        reservation_retained,
        "shared I/O reservation was released before confirmed cleanup"
    );
    assert!(
        source_retained,
        "original source ownership was released before confirmed cleanup"
    );
    assert_eq!(hashes(&source), before);
}
impl LegacyIoBudget for GrowWal {
    fn account(&self, _: u64, _: &LegacyCancellation) -> Result<()> {
        if !self.first.swap(true, Ordering::SeqCst) {
            self.file
                .lock()
                .unwrap()
                .write_all(&vec![0; self.bytes])
                .unwrap();
        }
        Ok(())
    }
}

#[tokio::test]
async fn wal_growth_after_cap_preflight_is_refused_before_copying_uncapped_bytes() {
    let directory = tempfile::tempdir().unwrap();
    let source = wal_source(directory.path());
    let cap = fs::metadata(&source).unwrap().len();
    let wal = append(&source, "-wal");
    let wal_bytes = fs::metadata(&wal).unwrap().len();
    assert!(wal_bytes < cap);
    // This fd models a raw, non-SQLite writer that ignores advisory ownership.
    // It grows the WAL after preflight while admission for the DB copy runs.
    let writer = OpenOptions::new().append(true).open(&wal).unwrap();
    let owner = PanOwner::open_owned(source.clone(), whole_readonly_lease(&source), true)
        .await
        .unwrap();
    let mut options = LegacyOptions {
        scratch_directory: directory.path().join("scratch"),
        ..LegacyOptions::default()
    };
    options.limits.max_database_bytes = cap;
    let budget = Arc::new(GrowWal {
        first: AtomicBool::new(false),
        file: Mutex::new(writer),
        bytes: (cap + 1 - wal_bytes) as usize,
    });
    let result = owner
        .snapshot(
            &options,
            None,
            false,
            LegacyCancellation::default(),
            Some(budget),
        )
        .await;
    let refused = matches!(
        result,
        Err(LegacyError::Stale | LegacyError::ResourceLimit(_))
    );
    drop(result);
    owner.close().await.unwrap();
    assert!(refused, "uncapped WAL growth was not refused");
    assert!(
        fs::read_dir(&options.scratch_directory)
            .unwrap()
            .next()
            .is_none()
    );
}

fn fifo(path: &Path) {
    use std::os::unix::ffi::OsStrExt;
    let path = std::ffi::CString::new(path.as_os_str().as_bytes()).unwrap();
    assert_eq!(unsafe { libc::mkfifo(path.as_ptr(), 0o600) }, 0);
}

#[test]
fn sidecar_replaced_by_fifo_after_metadata_never_blocks_or_opens_it() {
    use std::os::unix::fs::OpenOptionsExt;
    let directory = tempfile::tempdir().unwrap();
    let source = directory.path().join("source.pan-wal");
    fs::write(&source, b"owned regular journal").unwrap();
    let expected = fs::symlink_metadata(&source).unwrap();
    fs::remove_file(&source).unwrap();
    fifo(&source);
    let (sender, ready) = mpsc::channel();
    let pending = source.clone();
    let thread = thread::spawn(move || {
        let _ = sender.send(open_regular_source(&pending, &expected));
    });
    let first = ready.recv_timeout(Duration::from_secs(2));
    // Unblock the old blocking-open implementation before asserting RED.
    let unblock = if first.is_err() {
        Some(
            OpenOptions::new()
                .read(true)
                .write(true)
                .custom_flags(libc::O_NONBLOCK)
                .open(&source)
                .unwrap(),
        )
    } else {
        None
    };
    thread.join().unwrap();
    let returned_without_blocking = first.is_ok();
    let refused = first.map(|result| result.is_err()).unwrap_or(false);
    drop(unblock);
    drop(ready);
    fs::remove_file(&source).unwrap();
    assert!(
        returned_without_blocking && refused,
        "replacement FIFO was opened or blocked the source probe"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn canonical_path_replaced_by_fifo_returns_stale_without_opening_it() {
    use std::os::unix::fs::OpenOptionsExt;
    let directory = tempfile::tempdir().unwrap();
    let source = wal_source(directory.path());
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .open(&source)
        .unwrap();
    lock_source_byte(&file).unwrap();
    let owner = Arc::new(
        PanOwner::open_owned(source.clone(), Arc::new(file), false)
            .await
            .unwrap(),
    );
    let moved = directory.path().join("original-inode.pan");
    fs::rename(&source, &moved).unwrap();
    fifo(&source);
    let pending = owner.clone();
    let mut check = tokio::spawn(async move { pending.check(1).await });
    let first = tokio::time::timeout(Duration::from_secs(2), &mut check).await;
    let returned_without_blocking = first.is_ok();
    let (result, unblock) = match first {
        Ok(result) => (result, None),
        Err(_) => {
            let unblock = OpenOptions::new()
                .read(true)
                .write(true)
                .custom_flags(libc::O_NONBLOCK)
                .open(&source)
                .unwrap();
            (check.await, Some(unblock))
        }
    };
    drop(unblock);
    fs::remove_file(&source).unwrap();
    fs::rename(&moved, &source).unwrap();
    owner.close().await.unwrap();
    assert!(
        returned_without_blocking,
        "source identity probe blocked while opening a FIFO"
    );
    assert!(matches!(result.unwrap(), Err(LegacyError::Stale)));
}

#[tokio::test]
async fn refused_readonly_raw_copy_removes_its_stage_and_budget_reference() {
    let directory = tempfile::tempdir().unwrap();
    let source = wal_source(directory.path());
    let owner = PanOwner::open_owned(source.clone(), whole_readonly_lease(&source), true)
        .await
        .unwrap();
    let options = LegacyOptions {
        scratch_directory: directory.path().join("scratch"),
        ..LegacyOptions::default()
    };
    let before = hashes(&source);
    let budget = Arc::new(Denied);
    assert!(
        matches!(owner.snapshot(&options, None, false, LegacyCancellation::default(), Some(budget.clone())).await, Err(LegacyError::ResourceLimit(message)) if message == "Denied actual raw-copy chunk")
    );
    assert_eq!(Arc::strong_count(&budget), 1);
    assert!(
        fs::read_dir(&options.scratch_directory)
            .unwrap()
            .next()
            .is_none()
    );
    assert_eq!(hashes(&source), before);
    owner.close().await.unwrap();
}

struct Gate {
    first: AtomicBool,
    entered: Mutex<Option<mpsc::Sender<()>>>,
    release: Mutex<Option<mpsc::Receiver<()>>>,
}
impl LegacyIoBudget for Gate {
    fn account(&self, _: u64, _: &LegacyCancellation) -> Result<()> {
        if !self.first.swap(true, Ordering::SeqCst) {
            self.entered
                .lock()
                .unwrap()
                .take()
                .unwrap()
                .send(())
                .unwrap();
            self.release.lock().unwrap().take().unwrap().recv().unwrap();
        }
        Ok(())
    }
}
struct Release(Option<mpsc::Sender<()>>);
impl Release {
    fn open(&mut self) {
        self.0.take().unwrap().send(()).unwrap();
    }
}
impl Drop for Release {
    fn drop(&mut self) {
        if let Some(sender) = self.0.take() {
            let _ = sender.send(());
        }
    }
}
fn gate() -> (Arc<Gate>, mpsc::Receiver<()>, Release) {
    let (entered, ready) = mpsc::channel();
    let (release, receiver) = mpsc::channel();
    (
        Arc::new(Gate {
            first: AtomicBool::new(false),
            entered: Mutex::new(Some(entered)),
            release: Mutex::new(Some(receiver)),
        }),
        ready,
        Release(Some(release)),
    )
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn aborted_readonly_copy_retains_budget_and_original_lease_until_actual_cleanup() {
    let directory = tempfile::tempdir().unwrap();
    let source = wal_source(directory.path());
    let owner = Arc::new(
        PanOwner::open_owned(source.clone(), whole_readonly_lease(&source), true)
            .await
            .unwrap(),
    );
    let options = LegacyOptions {
        scratch_directory: directory.path().join("scratch"),
        ..LegacyOptions::default()
    };
    let before = hashes(&source);
    let (budget, ready, mut release) = gate();
    let cancel = LegacyCancellation::default();
    let pending_owner = owner.clone();
    let pending_options = options.clone();
    let pending_budget = budget.clone();
    let pending_cancel = cancel.clone();
    let waiter = tokio::spawn(async move {
        pending_owner
            .snapshot(
                &pending_options,
                None,
                false,
                pending_cancel,
                Some(pending_budget),
            )
            .await
    });
    tokio::task::spawn_blocking(move || ready.recv_timeout(Duration::from_secs(3)).unwrap())
        .await
        .unwrap();
    waiter.abort();
    let _ = waiter.await;
    cancel.cancel();
    assert!(Arc::strong_count(&budget) > 1);
    fs::set_permissions(&source, fs::Permissions::from_mode(0o644)).unwrap();
    probe_byte_in_process(&source, true);
    assert!(
        fs::read_dir(&options.scratch_directory)
            .unwrap()
            .next()
            .is_some()
    );
    let closing_owner = owner.clone();
    let close = tokio::spawn(async move { closing_owner.close().await });
    tokio::task::yield_now().await;
    assert!(!close.is_finished());
    release.open();
    close.await.unwrap().unwrap();
    assert_eq!(Arc::strong_count(&budget), 1);
    assert!(
        fs::read_dir(&options.scratch_directory)
            .unwrap()
            .next()
            .is_none()
    );
    assert_eq!(hashes(&source), before);
    drop(owner);
    probe_byte_in_process(&source, false);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn readonly_raw_copy_callback_returning_after_original_deadline_is_refused() {
    let directory = tempfile::tempdir().unwrap();
    let source = wal_source(directory.path());
    let owner = Arc::new(
        PanOwner::open_owned(source.clone(), whole_readonly_lease(&source), true)
            .await
            .unwrap(),
    );
    let options = LegacyOptions {
        scratch_directory: directory.path().join("scratch"),
        sql_timeout: Duration::from_millis(500),
        ..LegacyOptions::default()
    };
    let (budget, ready, mut release) = gate();
    let pending_owner = owner.clone();
    let pending_options = options.clone();
    let pending_budget = budget.clone();
    let waiter = tokio::spawn(async move {
        pending_owner
            .snapshot(
                &pending_options,
                None,
                false,
                LegacyCancellation::default(),
                Some(pending_budget),
            )
            .await
    });
    tokio::task::spawn_blocking(move || ready.recv_timeout(Duration::from_secs(3)).unwrap())
        .await
        .unwrap();
    tokio::time::sleep(Duration::from_millis(550)).await;
    assert!(!waiter.is_finished());
    release.open();
    assert!(matches!(
        waiter.await.unwrap(),
        Err(LegacyError::ResourceLimit(_))
    ));
    assert_eq!(Arc::strong_count(&budget), 1);
    assert!(
        fs::read_dir(&options.scratch_directory)
            .unwrap()
            .next()
            .is_none()
    );
    owner.close().await.unwrap();
}

#[tokio::test]
async fn readonly_owned_wal_snapshot_keeps_the_latest_commit_and_original_bytes() {
    eprintln!("Bundled SQLite {}", rusqlite::version());
    let directory = tempfile::tempdir().unwrap();
    let source = wal_source(directory.path());
    let lease = whole_readonly_lease(&source);
    assert!(OpenOptions::new().write(true).open(&source).is_err());
    let before = hashes(&source);
    let owner = PanOwner::open_owned(source.clone(), lease, true)
        .await
        .unwrap();
    let options = LegacyOptions {
        scratch_directory: directory.path().join("scratch"),
        ..LegacyOptions::default()
    };
    let charges = Arc::new(Charges::default());
    let snapshot = owner
        .snapshot(
            &options,
            None,
            false,
            LegacyCancellation::default(),
            Some(charges.clone()),
        )
        .await
        .unwrap();
    let db = Connection::open(snapshot.directory.path().join("workspace.pan")).unwrap();
    assert_eq!(
        db.query_row("SELECT recorded_at FROM panorama WHERE id=42", [], |row| {
            row.get::<_, String>(0)
        })
        .unwrap(),
        "WAL-only committed row"
    );
    db.close().unwrap();
    assert!(charges.calls.load(Ordering::SeqCst) > 0);
    assert!(charges.bytes.load(Ordering::SeqCst) > 0);
    let db_bytes = fs::metadata(&source).unwrap().len();
    let wal_bytes = fs::metadata(append(&source, "-wal")).unwrap().len();
    assert!(db_bytes <= 65536 && wal_bytes <= 65536);
    assert_eq!(
        &charges.chunks.lock().unwrap()[..4],
        &[db_bytes, db_bytes, wal_bytes, wal_bytes]
    );
    let mut files = fs::read_dir(snapshot.directory.path())
        .unwrap()
        .map(|entry| entry.unwrap().file_name())
        .collect::<Vec<_>>();
    files.sort();
    assert_eq!(files, ["snapshot.json", "workspace.pan"]);
    owner.check(snapshot.data_version).await.unwrap();
    assert!(matches!(
        owner
            .threshold(
                snapshot.data_version,
                vec![0; 8],
                vec![0; 8],
                LegacyCancellation::default(),
                None,
                options.sql_timeout
            )
            .await,
        Err(LegacyError::Sqlite(_))
    ));
    assert_eq!(hashes(&source), before);
    drop(snapshot);
    assert!(
        fs::read_dir(&options.scratch_directory)
            .unwrap()
            .next()
            .is_none()
    );
    owner.close().await.unwrap();
}
