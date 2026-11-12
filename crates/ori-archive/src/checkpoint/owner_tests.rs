use super::*;
use std::{fs, sync::atomic::AtomicUsize};

struct CompletionLatch {
    entered: AtomicBool,
    completed: AtomicUsize,
    largest: AtomicUsize,
    release: Mutex<mpsc::Receiver<()>>,
}
struct Budget(Arc<CompletionLatch>);
struct Permit(Arc<CompletionLatch>);
impl CheckpointBudget for Budget {
    fn try_begin(&self) -> Result<Option<Arc<dyn CheckpointIoPermit>>> {
        Ok(Some(Arc::new(Permit(self.0.clone()))))
    }
}
impl CheckpointIoPermit for Permit {
    fn account(&self, bytes: u64) -> Result<()> {
        self.0.largest.fetch_max(bytes as usize, Ordering::AcqRel);
        Ok(())
    }
    fn cancel(&self) {}
}
impl Drop for Permit {
    fn drop(&mut self) {
        // The connection and private VFS have already closed; only the owned
        // pass-completion handoff remains. No production latch/test knob.
        if self.0.completed.fetch_add(1, Ordering::AcqRel) == 0 {
            self.0.entered.store(true, Ordering::Release);
            self.0
                .release
                .lock()
                .unwrap()
                .recv_timeout(Duration::from_secs(5))
                .unwrap();
        }
    }
}
fn until(check: impl Fn() -> bool) -> bool {
    let deadline = Instant::now() + Duration::from_secs(3);
    while !check() && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(1));
    }
    check()
}

#[test]
fn real_wal_reset_reusing_the_frame_count_cannot_erase_a_later_commit_wakeup() {
    let root = tempfile::tempdir().unwrap();
    let path = root.path().join("same-size-wal.sqlite");
    let mut writer = Connection::open(&path).unwrap();
    writer.execute_batch("PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; CREATE TABLE t(id INTEGER PRIMARY KEY, payload BLOB)").unwrap();
    let initial = vec![0_u8; 32 * 1024];
    let tx = writer.transaction().unwrap();
    for id in 0..128 {
        tx.execute("INSERT INTO t VALUES(?,?)", rusqlite::params![id, &initial])
            .unwrap();
    }
    tx.commit().unwrap();
    writer
        .query_row("PRAGMA wal_checkpoint(TRUNCATE)", [], |r| {
            r.get::<_, i64>(0)
        })
        .unwrap();
    let (release, received) = mpsc::channel();
    let state = Arc::new(CompletionLatch {
        entered: AtomicBool::new(false),
        completed: AtomicUsize::new(0),
        largest: AtomicUsize::new(0),
        release: Mutex::new(received),
    });
    let (control, requests) = Control::new();
    let owner = Owner::start(
        control.clone(),
        requests,
        path.clone(),
        Arc::new(Budget(state.clone())),
    )
    .unwrap();
    control.install(&mut writer).unwrap();
    writer
        .execute("UPDATE t SET payload=?", [vec![0x11_u8; 32 * 1024]])
        .unwrap();
    let frames_a = control.frames.load(Ordering::Acquire);
    let entered = until(|| state.entered.load(Ordering::Acquire));
    let wal = path.with_extension("sqlite-wal");
    let salt_a = fs::read(&wal).unwrap()[16..24].to_vec();
    // All pages have the same size and positions. SQLite actually resets its
    // fully checkpointed WAL, then writes the same number of different pages.
    writer
        .execute("UPDATE t SET payload=?", [vec![0x22_u8; 32 * 1024]])
        .unwrap();
    let frames_b = control.frames.load(Ordering::Acquire);
    let salt_b = fs::read(&wal).unwrap()[16..24].to_vec();
    release.send(()).unwrap();
    let second_completed = until(|| state.completed.load(Ordering::Acquire) >= 2);
    owner.join();
    let failed = control.failed();
    drop(writer);
    // Stable main DB only: WAL is deliberately ignored AFTER both native
    // connections have closed, proving the second payload was backfilled.
    let uri = format!("file:{}?immutable=1", path.display());
    let main = Connection::open_with_flags(
        uri,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY | rusqlite::OpenFlags::SQLITE_OPEN_URI,
    )
    .unwrap();
    let copied_b: i64 = main
        .query_row(
            "SELECT COUNT(*) FROM t WHERE substr(payload,1,1)=X'22'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    drop(main);
    eprintln!(
        "real WAL reset: frames_a={frames_a}, frames_b={frames_b}, salt_changed={}, second_completed={second_completed}, copied_b={copied_b}",
        salt_a != salt_b
    );
    assert!(entered);
    assert!(frames_a >= AUTO_FRAMES);
    assert_eq!(
        frames_b, frames_a,
        "fixture must reproduce the real repeated WAL-frame count"
    );
    assert_ne!(
        salt_a, salt_b,
        "fixture must prove a real WAL reset, not an atomic counter edit"
    );
    assert!(
        second_completed && !failed,
        "the later commit lost its automatic maintenance wakeup"
    );
    assert_eq!(copied_b, 128);
    assert!(state.largest.load(Ordering::Acquire) <= vfs::MAX_IO_BYTES);
}

struct HeldCheckpoint {
    armed: AtomicBool,
    entered: AtomicBool,
    release: Mutex<mpsc::Receiver<()>>,
}
impl CheckpointIoPermit for HeldCheckpoint {
    fn account(&self, bytes: u64) -> Result<()> {
        if bytes == 0
            && self.armed.load(Ordering::Acquire)
            && !self.entered.swap(true, Ordering::AcqRel)
        {
            self.release
                .lock()
                .unwrap()
                .recv_timeout(Duration::from_secs(5))
                .map_err(|_| ArchiveError::WorkerStopped)?;
        }
        Ok(())
    }
    fn cancel(&self) {}
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn actual_sqlite_checkpoint_lock_contention_is_retryable_and_preserves_the_explicit_error() {
    let root = tempfile::tempdir().unwrap();
    let path = root.path().join("contended.sqlite");
    let archive = crate::Archive::open(crate::ArchiveConfig::new(&path)).unwrap();
    let fixture: Value = serde_json::from_str(include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../contracts/fixtures/measurement-v1.json"
    )))
    .unwrap();
    let mut session = fixture["session"].clone();
    session["created_at"] = json!("2026-11-11T12:00:00.123456Z");
    archive.create_session(session).await.unwrap();
    let original = archive
        .append_sweeps(vec![fixture["sweep"].clone()])
        .await
        .unwrap()
        .items[0]
        .clone();
    let (release, received) = mpsc::channel();
    let held = Arc::new(HeldCheckpoint {
        armed: AtomicBool::new(false),
        entered: AtomicBool::new(false),
        release: Mutex::new(received),
    });
    let external_path = path.clone();
    let external_held = held.clone();
    let external = thread::spawn(move || -> Result<()> {
        let vfs = vfs::Vfs::new(external_held.clone(), Arc::new(AtomicBool::new(false)))?;
        let db = vfs.connect(&external_path)?;
        // Open schema and WAL first. The first subsequent zero-byte check is
        // the WAL sync inside walCheckpoint, AFTER its real checkpoint lock.
        db.query_row("SELECT COUNT(*) FROM sweeps", [], |r| r.get::<_, i64>(0))?;
        external_held.armed.store(true, Ordering::Release);
        let outcome = db.query_row("PRAGMA wal_checkpoint(PASSIVE)", [], |r| r.get::<_, i64>(0));
        drop(db);
        vfs.finish()?;
        outcome?;
        Ok(())
    });
    let entered = until(|| held.entered.load(Ordering::Acquire));
    let contended = archive.checkpoint().await;
    let appended = archive
        .append_observations(vec![fixture["observation"].clone()])
        .await;
    let _ = release.send(());
    let external_result = external.join().unwrap();
    let retried = archive.checkpoint().await;
    let closed = archive.close().await;
    let reopened = crate::Archive::open(crate::ArchiveConfig::new(&path)).unwrap();
    let retained = reopened.get_sweep("fixture-sweep-α").await.unwrap();
    let observation = reopened
        .get_observation("fixture-observation-α")
        .await
        .unwrap();
    reopened.close().await.unwrap();
    assert!(entered && external_result.is_ok());
    // SQLite's PRAGMA preserves its original structured busy response rather
    // than surfacing sqlite3_wal_checkpoint_v2's BUSY as a statement error.
    assert_eq!(contended.unwrap()["busy"], 1);
    assert!(
        appended.is_ok(),
        "ordinary SQLite checkpoint contention made ingestion permanently unavailable"
    );
    assert!(retried.is_ok() && closed.is_ok());
    assert_eq!(retained, Some(original));
    assert_eq!(observation, Some(appended.unwrap().items[0].clone()));
}

#[test]
fn actual_sqlite_open_busy_does_not_poison_the_owned_executor_or_change_committed_data() {
    let root = tempfile::tempdir().unwrap();
    let path = root.path().join("exclusive-owner.sqlite");
    let mut writer = Connection::open(&path).unwrap();
    // A real exclusive native owner makes a second connection's schema/open
    // fail with SQLITE_BUSY. No mock return code or production VFS is changed.
    writer.execute_batch("PRAGMA journal_mode=WAL; PRAGMA locking_mode=EXCLUSIVE; CREATE TABLE source(id TEXT PRIMARY KEY, raw BLOB); INSERT INTO source VALUES('é-é',X'0000000000000080')").unwrap();
    let (control, requests) = Control::new();
    control.install(&mut writer).unwrap();
    let owner = Owner::start(
        control.clone(),
        requests,
        path.clone(),
        Arc::new(LocalBudget::default()),
    )
    .unwrap();
    let (sent, received) = oneshot::channel();
    control.enqueue(sent);
    let contended = received.blocking_recv().unwrap();
    let poisoned = control.failed();
    writer
        .execute(
            "INSERT INTO source VALUES('after-busy', X'010000000000F0BF')",
            [],
        )
        .unwrap();
    drop(writer);
    let mut writer = Connection::open(&path).unwrap();
    control.install(&mut writer).unwrap();
    let (sent, received) = oneshot::channel();
    control.enqueue(sent);
    let retried = received.blocking_recv().unwrap();
    owner.join();
    drop(writer);
    let reopened = Connection::open(&path).unwrap();
    let retained: Vec<(String, Vec<u8>)> = reopened
        .prepare("SELECT id,raw FROM source ORDER BY id")
        .unwrap()
        .query_map([], |r| Ok((r.get(0)?, r.get(1)?)))
        .unwrap()
        .collect::<std::result::Result<_, _>>()
        .unwrap();
    drop(reopened);
    eprintln!(
        "real native contention: first={contended:?}, poisoned={poisoned}, retry={retried:?}"
    );
    assert!(
        matches!(contended, Err(ArchiveError::Sql(rusqlite::Error::SqliteFailure(error, _))) if error.code == rusqlite::ErrorCode::DatabaseBusy)
    );
    assert!(
        !poisoned,
        "a transient SQLite BUSY permanently poisoned maintenance admission"
    );
    assert!(retried.is_ok());
    assert_eq!(
        retained,
        vec![
            ("after-busy".into(), vec![1, 0, 0, 0, 0, 0, 240, 191]),
            ("é-é".into(), vec![0, 0, 0, 0, 0, 0, 0, 128])
        ]
    );
}
