use ori_archive::{Archive, ArchiveConfig, ArchiveError, CheckpointBudget, CheckpointIoPermit};
use rusqlite::{Connection, OpenFlags};
use serde_json::{Value, json};
use std::{
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicUsize, Ordering},
        mpsc,
    },
    time::Duration,
};

struct Deferred;
impl CheckpointBudget for Deferred {
    fn try_begin(&self) -> ori_archive::Result<Option<Arc<dyn CheckpointIoPermit>>> {
        Ok(None)
    }
}

fn fixture() -> Value {
    serde_json::from_str(include_str!(
        "../../../contracts/fixtures/measurement-v1.json"
    ))
    .unwrap()
}

async fn seed(archive: &Archive) {
    let mut session = fixture()["session"].clone();
    session["created_at"] = json!("2026-11-11T12:00:00.123456Z");
    archive.create_session(session).await.unwrap();
}

async fn large_sources(archive: &Archive) {
    for first in (0..64).step_by(8) {
        let sweeps = (first..first + 8)
            .map(|i| {
                let mut value = fixture()["sweep"].clone();
                value["id"] = json!(format!("checkpoint-source-{i}"));
                value["frequencies_hz"] =
                    json!((0..4096).map(|x| 400_000_000_i64 + x).collect::<Vec<_>>());
                value["levels"] = json!(
                    (0..4096)
                        .map(|x| -99.125 + x as f64 / 8192.0)
                        .collect::<Vec<_>>()
                );
                value
            })
            .collect();
        archive.append_sweeps(sweeps).await.unwrap();
    }
}

async fn until(check: impl Fn() -> bool) {
    tokio::time::timeout(Duration::from_secs(3), async {
        while !check() {
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .unwrap();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn releasing_a_wal_reader_does_not_put_backfill_on_the_next_ingestion_commit() {
    let root = tempfile::tempdir().unwrap();
    let path = root.path().join("sources.sqlite");
    let archive =
        Archive::open_with_checkpoint_budget(ArchiveConfig::new(&path), Arc::new(Deferred))
            .unwrap();
    seed(&archive).await;
    let reader = Connection::open_with_flags(&path, OpenFlags::SQLITE_OPEN_READ_ONLY).unwrap();
    reader.execute_batch("BEGIN").unwrap();
    reader
        .query_row("SELECT COUNT(*) FROM sweeps", [], |r| r.get::<_, i64>(0))
        .unwrap();
    large_sources(&archive).await;
    let wal_bytes = std::fs::metadata(path.with_extension("sqlite-wal"))
        .unwrap()
        .len();
    let before = std::fs::metadata(&path).unwrap().len();
    reader.execute_batch("ROLLBACK").unwrap();
    drop(reader);
    let committed = archive
        .append_observations(vec![fixture()["observation"].clone()])
        .await
        .unwrap();
    let after = std::fs::metadata(&path).unwrap().len();
    let exact = archive
        .get_sweep("checkpoint-source-63")
        .await
        .unwrap()
        .unwrap();
    let observation = archive
        .get_observation("fixture-observation-α")
        .await
        .unwrap();
    archive.close().await.unwrap();
    let reopened = Archive::open(ArchiveConfig::new(&path)).unwrap();
    let retained = reopened
        .get_sweep("checkpoint-source-63")
        .await
        .unwrap()
        .unwrap();
    reopened.close().await.unwrap();
    assert!(
        wal_bytes > 1000 * 4096,
        "must cross SQLite's real auto-checkpoint boundary"
    );
    assert_eq!(committed.last_sequence, Some(66));
    assert!(observation.is_some());
    assert_eq!(
        retained, exact,
        "committed WAL must survive deferred maintenance and reopen"
    );
    assert_eq!(
        after, before,
        "ordinary COMMIT backfilled the main database after the final reader released"
    );
}

struct Held {
    entered: AtomicBool,
    cancelled: AtomicBool,
    active: AtomicUsize,
    largest_charge: AtomicUsize,
    release: Mutex<mpsc::Receiver<()>>,
}
struct Holding(Arc<Held>);
struct HeldPermit(Arc<Held>);
impl CheckpointBudget for Holding {
    fn try_begin(&self) -> ori_archive::Result<Option<Arc<dyn CheckpointIoPermit>>> {
        self.0.active.fetch_add(1, Ordering::AcqRel);
        Ok(Some(Arc::new(HeldPermit(self.0.clone()))))
    }
}
impl CheckpointIoPermit for HeldPermit {
    fn account(&self, bytes: u64) -> ori_archive::Result<()> {
        self.0
            .largest_charge
            .fetch_max(bytes as usize, Ordering::AcqRel);
        if bytes != 0 && !self.0.entered.swap(true, Ordering::AcqRel) {
            self.0
                .release
                .lock()
                .unwrap()
                .recv_timeout(Duration::from_secs(5))
                .unwrap();
        }
        if self.0.cancelled.load(Ordering::Acquire) {
            Err(ArchiveError::Closed)
        } else {
            Ok(())
        }
    }
    fn cancel(&self) {
        self.0.cancelled.store(true, Ordering::Release);
    }
}
impl Drop for HeldPermit {
    fn drop(&mut self) {
        self.0.active.fetch_sub(1, Ordering::AcqRel);
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn dropped_close_waiter_keeps_the_writer_lease_until_native_maintenance_cleanup_joins() {
    let root = tempfile::tempdir().unwrap();
    let path = root.path().join("sources.sqlite");
    let (release, released) = mpsc::channel();
    let held = Arc::new(Held {
        entered: AtomicBool::new(false),
        cancelled: AtomicBool::new(false),
        active: AtomicUsize::new(0),
        largest_charge: AtomicUsize::new(0),
        release: Mutex::new(released),
    });
    let archive = Archive::open_with_checkpoint_budget(
        ArchiveConfig::new(&path),
        Arc::new(Holding(held.clone())),
    )
    .unwrap();
    seed(&archive).await;
    let checkpoint_archive = archive.clone();
    let checkpoint = tokio::spawn(async move { checkpoint_archive.checkpoint().await });
    until(|| held.entered.load(Ordering::Acquire)).await;
    // Holding actual maintenance I/O cannot occupy the ingestion executor.
    tokio::time::timeout(
        Duration::from_secs(2),
        archive.append_observations(vec![fixture()["observation"].clone()]),
    )
    .await
    .unwrap()
    .unwrap();
    let closing = archive.clone();
    let waiter = tokio::spawn(async move { closing.close().await });
    until(|| held.cancelled.load(Ordering::Acquire)).await;
    let pending = !waiter.is_finished();
    let lease_retained = matches!(
        Archive::open(ArchiveConfig::new(&path)),
        Err(ArchiveError::AlreadyOpen)
    );
    let registered = held.active.load(Ordering::Acquire);
    waiter.abort();
    let _ = waiter.await;
    release.send(()).unwrap();
    let checkpoint_result = checkpoint.await.unwrap();
    archive.close().await.unwrap();
    let reopened = Archive::open(ArchiveConfig::new(&path)).unwrap();
    let source = reopened
        .get_observation("fixture-observation-α")
        .await
        .unwrap();
    reopened.close().await.unwrap();
    assert!(pending && lease_retained);
    assert_eq!(registered, 1);
    assert_eq!(held.active.load(Ordering::Acquire), 0);
    assert!(held.largest_charge.load(Ordering::Acquire) <= 64 * 1024);
    assert!(matches!(checkpoint_result, Err(ArchiveError::Closed)));
    assert!(source.is_some());
}

struct Broken;
impl CheckpointBudget for Broken {
    fn try_begin(&self) -> ori_archive::Result<Option<Arc<dyn CheckpointIoPermit>>> {
        Ok(Some(Arc::new(Self)))
    }
}
impl CheckpointIoPermit for Broken {
    fn account(&self, _: u64) -> ori_archive::Result<()> {
        Err(std::io::Error::other("synthetic checkpoint I/O failure").into())
    }
    fn cancel(&self) {}
}

#[tokio::test]
async fn real_maintenance_failure_is_sticky_and_committed_wal_remains_reopenable() {
    let root = tempfile::tempdir().unwrap();
    let path = root.path().join("sources.sqlite");
    let archive =
        Archive::open_with_checkpoint_budget(ArchiveConfig::new(&path), Arc::new(Broken)).unwrap();
    seed(&archive).await;
    let checkpoint = archive.checkpoint().await;
    let mutation = archive
        .append_observations(vec![fixture()["observation"].clone()])
        .await;
    let first_close = archive.close().await;
    let second_close = archive.close().await;
    let reopened = Archive::open(ArchiveConfig::new(&path)).unwrap();
    let source = reopened.get_session("fixture-сессия").await.unwrap();
    let unaccepted = reopened
        .get_observation("fixture-observation-α")
        .await
        .unwrap();
    reopened.close().await.unwrap();
    assert!(checkpoint.is_err() && first_close.is_err() && second_close.is_err());
    assert!(matches!(mutation, Err(ArchiveError::WorkerStopped)));
    assert!(source.is_some());
    assert!(unaccepted.is_none());
}

struct PressurePermit(Arc<AtomicBool>);
struct PressureBudget(Arc<AtomicBool>);
impl CheckpointBudget for PressureBudget {
    fn try_begin(&self) -> ori_archive::Result<Option<Arc<dyn CheckpointIoPermit>>> {
        Ok(Some(Arc::new(PressurePermit(self.0.clone()))))
    }
}
impl CheckpointIoPermit for PressurePermit {
    fn account(&self, _: u64) -> ori_archive::Result<()> {
        if self.0.load(Ordering::Acquire) {
            Err(ArchiveError::Closed)
        } else {
            Ok(())
        }
    }
    fn cancel(&self) {}
}

#[tokio::test]
async fn pressure_cancellation_is_retryable_without_losing_or_changing_committed_sources() {
    let root = tempfile::tempdir().unwrap();
    let path = root.path().join("sources.sqlite");
    let pressure = Arc::new(AtomicBool::new(true));
    let archive = Archive::open_with_checkpoint_budget(
        ArchiveConfig::new(&path),
        Arc::new(PressureBudget(pressure.clone())),
    )
    .unwrap();
    seed(&archive).await;
    let cancelled = archive.checkpoint().await;
    pressure.store(false, Ordering::Release);
    let original = archive
        .append_sweeps(vec![fixture()["sweep"].clone()])
        .await
        .unwrap();
    let completed = archive.checkpoint().await.unwrap();
    archive.close().await.unwrap();
    let reopened = Archive::open(ArchiveConfig::new(&path)).unwrap();
    let value = reopened.get_sweep("fixture-sweep-α").await.unwrap();
    reopened.close().await.unwrap();
    assert!(matches!(cancelled, Err(ArchiveError::Closed)));
    assert_eq!(completed["wal_frames"], completed["checkpointed_frames"]);
    assert_eq!(value, Some(original.items[0].clone()));
}

#[tokio::test]
async fn default_owned_maintenance_backfills_without_an_explicit_checkpoint_request() {
    let root = tempfile::tempdir().unwrap();
    let path = root.path().join("sources.sqlite");
    let archive = Archive::open(ArchiveConfig::new(&path)).unwrap();
    seed(&archive).await;
    let initial = std::fs::metadata(&path).unwrap().len();
    large_sources(&archive).await;
    until(|| std::fs::metadata(&path).unwrap().len() > initial + 1024 * 1024).await;
    let latest = archive.get_sweep("checkpoint-source-63").await.unwrap();
    archive.close().await.unwrap();
    assert!(latest.is_some());
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn explicit_checkpoint_keeps_its_original_writer_order_without_running_io_there() {
    let root = tempfile::tempdir().unwrap();
    let path = root.path().join("sources.sqlite");
    let archive = Archive::open(ArchiveConfig::new(&path)).unwrap();
    seed(&archive).await;
    let blocker = Connection::open(&path).unwrap();
    blocker.execute_batch("BEGIN IMMEDIATE").unwrap();
    let writing = archive.clone();
    let write = tokio::spawn(async move {
        writing
            .append_observations(vec![fixture()["observation"].clone()])
            .await
    });
    until(|| archive.queue_status().retained_bytes != 0).await;
    let checking = archive.clone();
    let checkpoint = tokio::spawn(async move { checking.checkpoint().await });
    tokio::time::sleep(Duration::from_millis(50)).await;
    let bypassed_the_admitted_write = checkpoint.is_finished();
    blocker.execute_batch("ROLLBACK").unwrap();
    drop(blocker);
    write.await.unwrap().unwrap();
    checkpoint.await.unwrap().unwrap();
    archive.close().await.unwrap();
    assert!(
        !bypassed_the_admitted_write,
        "explicit checkpoint bypassed a previously admitted canonical write"
    );
}

struct PartialBudget(Arc<AtomicUsize>);
struct PartialPermit {
    phase: Arc<AtomicUsize>,
    charged: AtomicUsize,
}
impl CheckpointBudget for PartialBudget {
    fn try_begin(&self) -> ori_archive::Result<Option<Arc<dyn CheckpointIoPermit>>> {
        if self.0.load(Ordering::Acquire) == 0 {
            return Ok(None);
        }
        Ok(Some(Arc::new(PartialPermit {
            phase: self.0.clone(),
            charged: AtomicUsize::new(0),
        })))
    }
}
impl CheckpointIoPermit for PartialPermit {
    fn account(&self, bytes: u64) -> ori_archive::Result<()> {
        let total = self.charged.fetch_add(bytes as usize, Ordering::AcqRel) + bytes as usize;
        if self.phase.load(Ordering::Acquire) == 1 && total > 1024 * 1024 {
            self.phase.store(0, Ordering::Release);
            return Err(ArchiveError::Closed);
        }
        Ok(())
    }
    fn cancel(&self) {
        self.phase.store(0, Ordering::Release);
    }
}

#[tokio::test]
async fn cancellation_after_partial_native_backfill_keeps_the_wal_authoritative_and_can_retry() {
    let root = tempfile::tempdir().unwrap();
    let path = root.path().join("sources.sqlite");
    let phase = Arc::new(AtomicUsize::new(0));
    let archive = Archive::open_with_checkpoint_budget(
        ArchiveConfig::new(&path),
        Arc::new(PartialBudget(phase.clone())),
    )
    .unwrap();
    seed(&archive).await;
    large_sources(&archive).await;
    let original = archive.get_sweep("checkpoint-source-63").await.unwrap();
    let before = std::fs::metadata(&path).unwrap().len();
    phase.store(1, Ordering::Release);
    let cancelled = archive.checkpoint().await;
    let partially_written = std::fs::metadata(&path).unwrap().len();
    let after_cancel = archive.get_sweep("checkpoint-source-63").await.unwrap();
    archive
        .append_observations(vec![fixture()["observation"].clone()])
        .await
        .unwrap();
    phase.store(2, Ordering::Release);
    let completed = archive.checkpoint().await.unwrap();
    archive.close().await.unwrap();
    let reopened = Archive::open(ArchiveConfig::new(&path)).unwrap();
    let after_reopen = reopened.get_sweep("checkpoint-source-63").await.unwrap();
    let observation = reopened
        .get_observation("fixture-observation-α")
        .await
        .unwrap();
    reopened.close().await.unwrap();
    assert!(matches!(cancelled, Err(ArchiveError::Closed)));
    assert!(
        partially_written > before,
        "control must interrupt actual main-database backfill"
    );
    assert_eq!(after_cancel, original);
    assert_eq!(after_reopen, original);
    assert!(observation.is_some());
    assert_eq!(completed["wal_frames"], completed["checkpointed_frames"]);
}
