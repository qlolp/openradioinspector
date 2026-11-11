use super::*;
use crate::{Archive, ArchiveConfig};
use serde_json::json;
use std::{sync::mpsc, time::Duration};

fn value(id: &str) -> serde_json::Value {
    json!({"id":id,"name":"Synthetic lifecycle barrier","state":"running",
        "created_at":"2026-11-11T00:00:00Z","started_at":"2026-11-11T00:00:01Z","streams":[]})
}
async fn opened() -> (tempfile::TempDir, Archive) {
    let root = tempfile::tempdir().unwrap();
    let archive = Archive::open(ArchiveConfig::new(root.path().join("source.sqlite"))).unwrap();
    archive.create_session(value("one")).await.unwrap();
    archive.create_session(value("two")).await.unwrap();
    (root, archive)
}
async fn create_many(archive: &Archive, prefix: &str, count: usize) {
    for first in (0..count).step_by(256) {
        archive
            .apply(
                (first..count.min(first + 256))
                    .map(|i| Mutation::CreateSession(value(&format!("{prefix}-{i}"))))
                    .collect(),
            )
            .await
            .unwrap();
    }
}
struct Release(Option<mpsc::Sender<()>>);
impl Drop for Release {
    fn drop(&mut self) {
        if let Some(sender) = self.0.take() {
            let _ = sender.send(());
        }
    }
}
fn pause(registry: &Registry, after_commit: bool) -> (mpsc::Receiver<()>, Release) {
    let (entered, ready) = mpsc::channel();
    let (release, wait) = mpsc::channel();
    let hook = Box::new(move || {
        entered.send(()).unwrap();
        wait.recv_timeout(Duration::from_secs(10)).unwrap();
    });
    let mut hooks = registry.shared.hooks.lock().unwrap();
    if after_commit {
        hooks.after_commit = Some(hook);
    } else {
        hooks.before_apply = Some(hook);
    }
    (ready, Release(Some(release)))
}
async fn entered(ready: mpsc::Receiver<()>) {
    tokio::task::spawn_blocking(move || ready.recv_timeout(Duration::from_secs(10)))
        .await
        .unwrap()
        .unwrap();
}
async fn queued(archive: &Archive) {
    tokio::time::timeout(Duration::from_secs(10), async {
        while archive.queue_status().queued_operations == 0 {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn sql_visible_stop_is_pending_before_delayed_publication_without_blocking_other_session_checks()
 {
    let (_root, archive) = opened().await;
    let one = archive.capture_session_lifecycle("one").await.unwrap();
    let two = archive.capture_session_lifecycle("two").await.unwrap();
    let original = archive.get_session("one").await.unwrap().unwrap();
    let (ready, release) = pause(&archive.inner.lifecycle, true);
    let writer = archive.clone();
    let stopped =
        tokio::spawn(async move { writer.stop_session(original, "2026-11-11T00:00:05Z").await });
    entered(ready).await;
    // The real WAL transaction is already committed and visible on another
    // connection, while writer publication is deterministically paused.
    assert_eq!(
        archive.get_session("one").await.unwrap().unwrap()["state"],
        "stopped"
    );
    assert!(
        !one.check(),
        "SQL-visible stop left the old cache-only receipt valid"
    );
    assert!(archive.cached_session_lifecycle("one").is_none());
    assert!(two.check());
    assert!(archive.cached_session_lifecycle("two").unwrap().check());
    drop(release);
    stopped.await.unwrap().unwrap();
    assert!(!one.check());
    archive.close().await.unwrap();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn queued_capture_reads_the_earlier_writer_commit_instead_of_a_stale_reader_snapshot() {
    let (_root, archive) = opened().await;
    let original = archive.get_session("one").await.unwrap().unwrap();
    let old = archive.capture_session_lifecycle("one").await.unwrap();
    let (ready, release) = pause(&archive.inner.lifecycle, false);
    let writer = archive.clone();
    let stopped =
        tokio::spawn(async move { writer.stop_session(original, "2026-11-11T00:00:05Z").await });
    entered(ready).await;
    let capturing = archive.clone();
    let capture = tokio::spawn(async move { capturing.capture_session_lifecycle("one").await });
    queued(&archive).await;
    assert!(!capture.is_finished());
    assert!(old.check());
    assert_eq!(
        archive.get_session("one").await.unwrap().unwrap()["state"],
        "running"
    );
    drop(release);
    stopped.await.unwrap().unwrap();
    let after = capture.await.unwrap().unwrap();
    assert!(!after.check() && !old.check());
    assert!(archive.cached_session_lifecycle("one").is_none());
    archive.close().await.unwrap();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn close_invalidates_immediately_and_queued_capture_cannot_enroll_after_close() {
    let (_root, archive) = opened().await;
    let old = archive.capture_session_lifecycle("one").await.unwrap();
    let mut same = archive.get_session("one").await.unwrap().unwrap();
    same["name"] = json!("Queued metadata only");
    let (ready, release) = pause(&archive.inner.lifecycle, false);
    let writer = archive.clone();
    let saved = tokio::spawn(async move { writer.save_session(same, None).await });
    entered(ready).await;
    let capturing = archive.clone();
    let capture = tokio::spawn(async move { capturing.capture_session_lifecycle("one").await });
    queued(&archive).await;
    let closing = archive.clone();
    let closed = tokio::spawn(async move { closing.close().await });
    tokio::time::timeout(Duration::from_secs(10), async {
        while !archive.queue_status().closing {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    assert!(!old.check());
    assert!(archive.cached_session_lifecycle("one").is_none());
    drop(release);
    saved.await.unwrap().unwrap();
    assert!(matches!(capture.await.unwrap(), Err(ArchiveError::Closed)));
    closed.await.unwrap().unwrap();
    assert_eq!(archive.queue_status().retained_bytes, 0);
}

#[tokio::test]
async fn actual_deferred_foreign_key_commit_failure_restores_only_the_proven_rollback_generation() {
    let (_root, archive) = opened().await;
    let one = archive.capture_session_lifecycle("one").await.unwrap();
    let two = archive.capture_session_lifecycle("two").await.unwrap();
    let original = archive.get_session("one").await.unwrap().unwrap();
    let before = archive.events(0, 100).await.unwrap();
    let checking = one.clone();
    let unrelated = two.clone();
    archive
        .inner
        .lifecycle
        .shared
        .hooks
        .lock()
        .unwrap()
        .before_commit = Some(Box::new(move |db| {
        assert!(!checking.check());
        assert!(unrelated.check());
        db.execute_batch("PRAGMA defer_foreign_keys=ON; INSERT INTO sweeps VALUES('failed-commit','missing-session','missing-stream','2026-11-11T00:00:02Z','2026-11-11T00:00:03Z','[100000001]','[-0.0]','dBm','{}')").unwrap();
    }));
    assert!(matches!(
        archive
            .stop_session(original.clone(), "2026-11-11T00:00:05Z")
            .await,
        Err(ArchiveError::Sql(_))
    ));
    assert!(one.check() && two.check());
    assert!(archive.cached_session_lifecycle("one").unwrap().check());
    assert_eq!(archive.get_session("one").await.unwrap().unwrap(), original);
    assert_eq!(archive.events(0, 100).await.unwrap(), before);
    assert!(archive.get_sweep("failed-commit").await.unwrap().is_none());
    archive.close().await.unwrap();
}

#[tokio::test]
async fn a_thousand_unused_enrollments_leave_no_id_map_growth_or_archive_ownership() {
    let (_root, archive) = opened().await;
    create_many(&archive, "unused", 1000).await;
    let active = archive.capture_session_lifecycle("one").await.unwrap();
    for i in 0..1000 {
        let id = format!("unused-{i}");
        let receipt = archive.capture_session_lifecycle(&id).await.unwrap();
        assert!(receipt.check());
        drop(receipt);
        assert!(archive.cached_session_lifecycle(&id).is_none());
        let state = archive.inner.lifecycle.shared.state.lock().unwrap();
        assert_eq!(state.cells.len(), 1);
        assert_eq!(state.key_bytes, "one".len());
    }
    drop(active);
    assert!(
        archive
            .inner
            .lifecycle
            .shared
            .state
            .lock()
            .unwrap()
            .cells
            .is_empty()
    );
    archive.close().await.unwrap();
}

#[tokio::test]
async fn live_enrollments_have_an_explicit_bound_instead_of_silent_eviction() {
    let (_root, archive) = opened().await;
    create_many(&archive, "live", MAX_CELLS + 1).await;
    let mut receipts = Vec::new();
    for i in 0..MAX_CELLS {
        receipts.push(
            archive
                .capture_session_lifecycle(&format!("live-{i}"))
                .await
                .unwrap(),
        );
    }
    assert!(matches!(
        archive
            .capture_session_lifecycle(&format!("live-{MAX_CELLS}"))
            .await,
        Err(ArchiveError::QueueFull { .. })
    ));
    assert!(receipts.iter().all(SessionLifecycleReceipt::check));
    drop(receipts);
    assert!(
        archive
            .inner
            .lifecycle
            .shared
            .state
            .lock()
            .unwrap()
            .cells
            .is_empty()
    );
    assert!(
        archive
            .capture_session_lifecycle(&format!("live-{MAX_CELLS}"))
            .await
            .unwrap()
            .check()
    );
    archive.close().await.unwrap();
}

#[tokio::test]
async fn retained_exact_id_bytes_have_a_separate_bound_and_release_on_drop() {
    let (_root, archive) = opened().await;
    let id_bytes = MAX_KEY_BYTES / 8;
    let ids = (0..9)
        .map(|i| format!("{}{i}", "e\u{301}".repeat((id_bytes - 2) / 3)))
        .collect::<Vec<_>>();
    assert!(ids.iter().all(|id| id.len() <= id_bytes));
    // Use exact-length, unnormalized keys so the byte cap is independent of
    // the much larger live-cell cap.
    let ids = ids
        .into_iter()
        .map(|mut id| {
            id.push_str(&"x".repeat(id_bytes - id.len()));
            id
        })
        .collect::<Vec<_>>();
    archive
        .apply(
            ids.iter()
                .map(|id| Mutation::CreateSession(value(id)))
                .collect(),
        )
        .await
        .unwrap();
    let mut receipts = Vec::new();
    for id in &ids[..8] {
        receipts.push(archive.capture_session_lifecycle(id).await.unwrap());
    }
    assert_eq!(
        archive
            .inner
            .lifecycle
            .shared
            .state
            .lock()
            .unwrap()
            .key_bytes,
        MAX_KEY_BYTES
    );
    assert!(matches!(
        archive.capture_session_lifecycle(&ids[8]).await,
        Err(ArchiveError::QueueFull { .. })
    ));
    assert!(receipts.iter().all(SessionLifecycleReceipt::check));
    drop(receipts.pop());
    assert!(
        archive
            .capture_session_lifecycle(&ids[8])
            .await
            .unwrap()
            .check()
    );
    archive.close().await.unwrap();
}

#[tokio::test]
async fn a_receipt_does_not_keep_the_archive_owner_alive_after_final_drop() {
    let (_root, archive) = opened().await;
    let receipt = archive.capture_session_lifecycle("one").await.unwrap();
    let owner = Arc::downgrade(&archive.inner);
    drop(archive);
    assert!(owner.upgrade().is_none());
    assert!(!receipt.check());
}

#[tokio::test]
async fn poisoned_enrollment_registry_cannot_bypass_tracking_and_commit_a_stop() {
    let (_root, archive) = opened().await;
    let receipt = archive.capture_session_lifecycle("one").await.unwrap();
    let original = archive.get_session("one").await.unwrap().unwrap();
    let registry = archive.inner.lifecycle.clone();
    assert!(
        std::thread::spawn(move || {
            let _state = registry.shared.state.lock().unwrap();
            panic!("synthetic poisoned registry");
        })
        .join()
        .is_err()
    );
    assert!(archive.cached_session_lifecycle("one").is_none());
    assert!(matches!(
        archive
            .stop_session(original.clone(), "2026-11-11T00:00:05Z")
            .await,
        Err(ArchiveError::WorkerStopped)
    ));
    assert!(receipt.check());
    assert_eq!(archive.get_session("one").await.unwrap().unwrap(), original);
    archive.close().await.unwrap();
    assert!(!receipt.check());
}
