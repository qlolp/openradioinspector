use ori_archive::{
    Archive, ArchiveConfig, ArchiveError, BackupCancellation, BackupIoBudget, BackupOptions,
    restore_backup,
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    fs,
    path::Path,
    sync::{
        Arc, Mutex,
        atomic::{AtomicUsize, Ordering},
        mpsc,
    },
    time::Duration,
};

fn options() -> BackupOptions {
    BackupOptions {
        max_snapshot_bytes: 1024 * 1024,
        copy_timeout: Duration::from_secs(2),
        total_timeout: Duration::from_secs(5),
        cancel: BackupCancellation::default(),
    }
}
async fn seeded() -> (tempfile::TempDir, Archive, Value) {
    let root = tempfile::tempdir().unwrap();
    let archive = Archive::open(ArchiveConfig::new(root.path().join("source.sqlite"))).unwrap();
    let fixture = ori_archive::parse_json_value(
        include_bytes!("../../../contracts/fixtures/measurement-v1.json"),
        1024 * 1024,
    )
    .unwrap();
    let mut session = fixture["session"].clone();
    session["created_at"] = json!("2026-11-11T12:00:00.123456Z");
    archive.create_session(session).await.unwrap();
    let mut sweep = fixture["sweep"].clone();
    sweep["id"] = json!("é-é-😀/exact");
    sweep["levels"][0] = json!(-0.0);
    sweep["interval"] = json!({"started_at":"2026-11-11T12:00:00.123456Z", "ended_at":"2026-11-11T12:00:00.223456Z"});
    archive.append_sweeps(vec![sweep.clone()]).await.unwrap();
    (root, archive, sweep)
}
fn no_stages(root: &Path) -> bool {
    fs::read_dir(root).unwrap().all(|entry| {
        !entry
            .unwrap()
            .file_name()
            .to_string_lossy()
            .starts_with(".ori-recovery-")
    })
}
fn source_hashes(root: &Path) -> Vec<Vec<u8>> {
    ["source.sqlite", "source.sqlite-wal"]
        .iter()
        .map(|name| Sha256::digest(fs::read(root.join(name)).unwrap()).to_vec())
        .collect()
}
struct Deny;
impl BackupIoBudget for Deny {
    fn account(&self, _: u64) -> ori_archive::Result<()> {
        Err(ArchiveError::QueryBudget)
    }
}

#[tokio::test]
async fn denied_io_never_publishes_or_changes_the_original_source() {
    let (root, archive, source) = seeded().await;
    let before = archive.events(0, 100).await.unwrap();
    let hashes = source_hashes(root.path());
    let target = root.path().join("denied");
    assert!(matches!(
        archive
            .backup_with_io_budget(&target, options(), Arc::new(Deny))
            .await,
        Err(ArchiveError::QueryBudget)
    ));
    assert!(!target.exists());
    assert!(no_stages(root.path()));
    assert_eq!(archive.events(0, 100).await.unwrap(), before);
    assert_eq!(source_hashes(root.path()), hashes);
    assert_eq!(
        archive
            .get_sweep(source["id"].as_str().unwrap())
            .await
            .unwrap(),
        Some(source)
    );
    archive
        .backup_with_options(&root.path().join("old-api"), options())
        .await
        .unwrap();
    archive.close().await.unwrap();
}

struct DenyManifest(AtomicUsize);
impl BackupIoBudget for DenyManifest {
    fn account(&self, _: u64) -> ori_archive::Result<()> {
        if self.0.fetch_add(1, Ordering::SeqCst) == 4 {
            Err(ArchiveError::QueryBudget)
        } else {
            Ok(())
        }
    }
}
#[tokio::test]
async fn denied_manifest_write_preserves_budget_error_and_removes_the_sealed_stage() {
    let (root, archive, _) = seeded().await;
    let target = root.path().join("refused-manifest");
    assert!(matches!(
        archive
            .backup_with_io_budget(
                &target,
                options(),
                Arc::new(DenyManifest(AtomicUsize::new(0)))
            )
            .await,
        Err(ArchiveError::QueryBudget)
    ));
    assert!(!target.exists());
    assert!(no_stages(root.path()));
    archive.close().await.unwrap();
}

struct Record {
    root: std::path::PathBuf,
    calls: Mutex<Vec<(u64, u64)>>,
}
impl BackupIoBudget for Record {
    fn account(&self, bytes: u64) -> ori_archive::Result<()> {
        let copy_bytes = fs::read_dir(&self.root)?
            .filter_map(|entry| entry.ok())
            .find(|entry| {
                entry
                    .file_name()
                    .to_string_lossy()
                    .starts_with(".ori-recovery-")
            })
            .and_then(|entry| fs::metadata(entry.path().join("archive.sqlite")).ok())
            .map_or(0, |info| info.len());
        self.calls.lock().unwrap().push((bytes, copy_bytes));
        Ok(())
    }
}
#[tokio::test]
async fn callback_precedes_copy_read_and_write_and_preserves_exact_records() {
    let (root, archive, expected) = seeded().await;
    let bytes = archive.capacity().await.unwrap()["database_bytes"]
        .as_u64()
        .unwrap();
    assert!(bytes < 512 * 1024);
    let budget = Arc::new(Record {
        root: root.path().to_owned(),
        calls: Mutex::new(Vec::new()),
    });
    let target = root.path().join("recovery");
    archive
        .backup_with_io_budget(&target, options(), budget.clone())
        .await
        .unwrap();
    let calls = budget.calls.lock().unwrap().clone();
    assert!(calls.len() > 2);
    assert_eq!(&calls[..2], &[(bytes, 0), (bytes, 0)]);
    assert!(
        calls
            .iter()
            .all(|(charge, _)| (1..=1024 * 1024).contains(charge))
    );
    let manifest_bytes = fs::metadata(target.join("manifest.json")).unwrap().len();
    assert_eq!(
        calls.iter().map(|(bytes, _)| bytes).sum::<u64>(),
        6 * bytes + 2 * manifest_bytes
    );
    let restored = root.path().join("restored.sqlite");
    restore_backup(&target, &restored).unwrap();
    let reopened = Archive::open(ArchiveConfig::new(restored)).unwrap();
    let actual = reopened
        .get_sweep(expected["id"].as_str().unwrap())
        .await
        .unwrap()
        .unwrap();
    assert_eq!(actual["id"], expected["id"]);
    assert_eq!(actual["interval"], expected["interval"]);
    assert_eq!(actual["frequencies_hz"], expected["frequencies_hz"]);
    for (actual, expected) in actual["levels"]
        .as_array()
        .unwrap()
        .iter()
        .zip(expected["levels"].as_array().unwrap())
    {
        assert_eq!(
            actual.as_f64().unwrap().to_bits(),
            expected.as_f64().unwrap().to_bits()
        );
    }
    assert_eq!(
        reopened.events(0, 100).await.unwrap(),
        archive.events(0, 100).await.unwrap()
    );
    reopened.close().await.unwrap();
    archive.close().await.unwrap();
}

#[tokio::test]
async fn artifact_hashes_and_index_reads_and_writes_are_all_charged() {
    let root = tempfile::tempdir().unwrap();
    let path = root.path().join("source.sqlite");
    fs::copy(
        concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/tests/fixtures/legacy_v5_utf16.sqlite"
        ),
        &path,
    )
    .unwrap();
    let archive = Archive::open(ArchiveConfig::new(path)).unwrap();
    let bytes = archive.capacity().await.unwrap()["database_bytes"]
        .as_u64()
        .unwrap();
    let artifact = archive
        .get_artifact("fixture-сессия", "receiver+one", "synthetic-audio")
        .await
        .unwrap()
        .unwrap();
    let budget = Arc::new(Record {
        root: root.path().to_owned(),
        calls: Mutex::new(Vec::new()),
    });
    let target = root.path().join("with-artifact");
    archive
        .backup_with_io_budget(&target, options(), budget.clone())
        .await
        .unwrap();
    let index = fs::metadata(target.join("artifacts.ndjson")).unwrap().len();
    let manifest = fs::metadata(target.join("manifest.json")).unwrap().len();
    let charged = budget
        .calls
        .lock()
        .unwrap()
        .iter()
        .map(|(bytes, _)| bytes)
        .sum::<u64>();
    assert_eq!(
        charged,
        6 * bytes + 2 * artifact.payload.len() as u64 + 4 * index + 2 * manifest
    );
    let restored = root.path().join("restored.sqlite");
    restore_backup(&target, &restored).unwrap();
    let reopened = Archive::open(ArchiveConfig::new(restored)).unwrap();
    let actual = reopened
        .get_artifact("fixture-сессия", "receiver+one", "synthetic-audio")
        .await
        .unwrap()
        .unwrap();
    assert_eq!(actual.payload, artifact.payload);
    assert_eq!(actual.sha256, artifact.sha256);
    reopened.close().await.unwrap();
    archive.close().await.unwrap();
}

struct CancelAtCallback(BackupCancellation);
impl BackupIoBudget for CancelAtCallback {
    fn account(&self, _: u64) -> ori_archive::Result<()> {
        self.0.cancel();
        Ok(())
    }
}
#[tokio::test]
async fn cancellation_inside_callback_is_rechecked_before_any_copy() {
    let (root, archive, _) = seeded().await;
    let policy = options();
    let cancel = policy.cancel.clone();
    let target = root.path().join("cancelled");
    assert!(matches!(
        archive
            .backup_with_io_budget(&target, policy, Arc::new(CancelAtCallback(cancel)))
            .await,
        Err(ArchiveError::Closed)
    ));
    assert!(!target.exists());
    assert!(no_stages(root.path()));
    archive.close().await.unwrap();
}

struct Blocking {
    entered: Mutex<Option<mpsc::Sender<()>>>,
    release: Mutex<mpsc::Receiver<()>>,
}
impl BackupIoBudget for Blocking {
    fn account(&self, _: u64) -> ori_archive::Result<()> {
        if let Some(entered) = self.entered.lock().unwrap().take() {
            entered.send(()).unwrap();
            self.release.lock().unwrap().recv().unwrap();
        }
        Ok(())
    }
}
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn callback_returning_after_copy_deadline_cannot_acknowledge_a_bundle() {
    let (root, archive, _) = seeded().await;
    let (entered, ready) = mpsc::channel();
    let (release, gate) = mpsc::channel();
    let budget = Arc::new(Blocking {
        entered: Mutex::new(Some(entered)),
        release: Mutex::new(gate),
    });
    let mut policy = options();
    policy.copy_timeout = Duration::from_millis(150);
    let target = root.path().join("late-callback");
    let destination = target.clone();
    let worker = archive.clone();
    let waiter = tokio::spawn(async move {
        worker
            .backup_with_io_budget(&destination, policy, budget)
            .await
    });
    tokio::task::spawn_blocking(move || ready.recv_timeout(Duration::from_secs(2)).unwrap())
        .await
        .unwrap();
    tokio::time::sleep(Duration::from_millis(200)).await;
    assert!(
        !waiter.is_finished(),
        "A cooperative callback must drain before acknowledgement"
    );
    release.send(()).unwrap();
    assert!(matches!(
        waiter.await.unwrap(),
        Err(ArchiveError::QueryBudget)
    ));
    assert!(!target.exists());
    assert!(no_stages(root.path()));
    archive.close().await.unwrap();
}
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn aborted_waiter_keeps_budget_and_backup_slot_until_sql_cleanup_finishes() {
    let (root, archive, _) = seeded().await;
    let (entered, ready) = mpsc::channel();
    let (release, gate) = mpsc::channel();
    let budget = Arc::new(Blocking {
        entered: Mutex::new(Some(entered)),
        release: Mutex::new(gate),
    });
    let retained = Arc::downgrade(&budget);
    let policy = options();
    let cancel = policy.cancel.clone();
    let target = root.path().join("aborted");
    let destination = target.clone();
    let worker = archive.clone();
    let waiter = tokio::spawn(async move {
        worker
            .backup_with_io_budget(&destination, policy, budget)
            .await
    });
    tokio::task::spawn_blocking(move || ready.recv_timeout(Duration::from_secs(2)).unwrap())
        .await
        .unwrap();
    waiter.abort();
    let _ = waiter.await;
    cancel.cancel();
    assert!(retained.upgrade().is_some());
    assert!(matches!(
        archive.backup(&root.path().join("parallel")).await,
        Err(ArchiveError::QueueFull { .. })
    ));
    let closing = archive.clone();
    let close = tokio::spawn(async move { closing.close().await });
    while !archive.queue_status().closing {
        tokio::task::yield_now().await;
    }
    assert!(!close.is_finished());
    release.send(()).unwrap();
    tokio::time::timeout(Duration::from_secs(2), close)
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    assert!(retained.upgrade().is_none());
    assert!(!target.exists());
    assert!(no_stages(root.path()));
}
