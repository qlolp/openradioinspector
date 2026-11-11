use ori_archive::{Archive, ArchiveConfig, ArchiveError, EventDraft, Mutation};
use serde_json::{Value, json};
use std::time::{Duration, Instant};
fn fixture() -> Value {
    ori_archive::parse_json_value(
        include_bytes!("../../../contracts/fixtures/measurement-v1.json"),
        1024 * 1024,
    )
    .unwrap()
}
fn session() -> Value {
    let mut value = fixture()["session"].clone();
    value["created_at"] = json!("2026-11-11T12:00:00Z");
    value
}
fn source(id: &str) -> Value {
    let mut value = fixture()["sweep"].clone();
    value["id"] = json!(id);
    value
}
async fn wait_until(predicate: impl Fn() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(2);
    while !predicate() {
        assert!(
            Instant::now() < deadline,
            "writer queue did not reach expected state"
        );
        tokio::task::yield_now().await;
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn full_writer_queue_rejects_explicitly_readers_do_not_wait_for_the_writer_and_notices_follow_commit()
 {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("archive.sqlite");
    let mut config = ArchiveConfig::new(&path);
    config.writer_queue_capacity = 1;
    let archive = Archive::open(config).unwrap();
    archive.create_session(session()).await.unwrap();
    let mut notices = archive.subscribe_commits();
    let blocker = rusqlite::Connection::open(&path).unwrap();
    blocker.execute_batch("BEGIN IMMEDIATE").unwrap();
    let one = archive.clone();
    let first =
        tokio::spawn(async move { one.append_sweeps(vec![source("accepted-first")]).await });
    wait_until(|| {
        archive.queue_status().retained_bytes > 0 && archive.queue_status().queued_operations == 0
    })
    .await;
    let two = archive.clone();
    let second =
        tokio::spawn(async move { two.append_sweeps(vec![source("accepted-second")]).await });
    wait_until(|| archive.queue_status().queued_operations == 1).await;
    assert!(matches!(
        archive.append_sweeps(vec![source("rejected-third")]).await,
        Err(ArchiveError::QueueFull { .. })
    ));
    assert!(
        tokio::time::timeout(Duration::from_millis(30), notices.recv())
            .await
            .is_err()
    );
    let read = tokio::time::timeout(
        Duration::from_millis(500),
        archive.get_session("fixture-сессия"),
    )
    .await
    .unwrap()
    .unwrap();
    assert!(read.is_some());
    assert!(archive.get_sweep("accepted-first").await.unwrap().is_none());
    blocker.execute_batch("COMMIT").unwrap();
    first.await.unwrap().unwrap();
    second.await.unwrap().unwrap();
    let notice = notices.recv().await.unwrap();
    assert_eq!(notice.first_sequence, 2);
    assert!(archive.get_sweep("accepted-first").await.unwrap().is_some());
    assert!(archive.get_sweep("rejected-third").await.unwrap().is_none());
    assert_eq!(archive.events(0, 100).await.unwrap().len(), 3);
    archive.close().await.unwrap();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn concurrent_source_batches_have_contiguous_transaction_ranges_and_no_partial_acknowledgements()
 {
    let dir = tempfile::tempdir().unwrap();
    let archive = Archive::open(ArchiveConfig::new(dir.path().join("archive.sqlite"))).unwrap();
    archive.create_session(session()).await.unwrap();
    let mut tasks = Vec::new();
    for batch in 0..16 {
        let owner = archive.clone();
        tasks.push(tokio::spawn(async move {
            owner
                .append_sweeps(
                    (0..8)
                        .map(|item| source(&format!("batch-{batch}-{item}")))
                        .collect(),
                )
                .await
                .unwrap()
        }));
    }
    let mut all_sequences = Vec::new();
    for task in tasks {
        let committed = task.await.unwrap();
        let ids = committed
            .events
            .iter()
            .map(|e| e["sequence"].as_u64().unwrap())
            .collect::<Vec<_>>();
        assert!(ids.windows(2).all(|w| w[1] == w[0] + 1));
        assert_eq!(committed.items.len(), 8);
        all_sequences.extend(ids);
    }
    all_sequences.sort();
    assert_eq!(all_sequences, (2..=129).collect::<Vec<_>>());
    assert_eq!(
        archive
            .sweep_ingestion_boundary("fixture-сессия")
            .await
            .unwrap(),
        128
    );
    assert_eq!(
        archive
            .list_sweeps("fixture-сессия", None, 1000)
            .await
            .unwrap()
            .len(),
        128
    );
    archive.close().await.unwrap();
}

#[tokio::test]
async fn capacity_rollback_keeps_lifecycle_metadata_writable_and_never_publishes_rejected_measurements()
 {
    let dir = tempfile::tempdir().unwrap();
    let mut config = ArchiveConfig::new(dir.path().join("archive.sqlite"));
    config.max_database_bytes = Some(1);
    let archive = Archive::open(config).unwrap();
    archive.create_session(session()).await.unwrap();
    let mut notices = archive.subscribe_commits();
    assert!(matches!(
        archive.append_sweeps(vec![source("over-capacity")]).await,
        Err(ArchiveError::Capacity { .. })
    ));
    assert!(archive.get_sweep("over-capacity").await.unwrap().is_none());
    assert_eq!(archive.events(0, 100).await.unwrap().len(), 1);
    assert!(notices.try_recv().is_err());
    let mut stopped = archive
        .get_session("fixture-сессия")
        .await
        .unwrap()
        .unwrap();
    stopped["state"] = json!("stopped");
    stopped["ended_at"] = json!("2026-11-11T12:01:00Z");
    archive
        .save_session(
            stopped,
            Some(EventDraft::now(
                "session.stopped",
                Some("fixture-сессия".into()),
                json!({}),
            )),
        )
        .await
        .unwrap();
    assert_eq!(archive.events(0, 100).await.unwrap().len(), 2);
    assert_eq!(archive.capacity().await.unwrap()["state"], "full");
    archive.close().await.unwrap();
}

#[tokio::test]
async fn byte_budget_and_response_budget_are_explicit_errors_and_never_drop_or_truncate_rows() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("archive.sqlite");
    let archive = Archive::open(ArchiveConfig::new(&path)).unwrap();
    archive.create_session(session()).await.unwrap();
    archive.close().await.unwrap();
    let mut config = ArchiveConfig::new(&path);
    config.writer_queue_bytes = 64;
    config.max_response_bytes = 100;
    let archive = Archive::open(config).unwrap();
    assert!(matches!(
        archive.append_sweeps(vec![source("byte-rejected")]).await,
        Err(ArchiveError::QueueFull { .. })
    ));
    assert!(matches!(
        archive.get_session("fixture-сессия").await,
        Err(ArchiveError::QueryBudget)
    ));
    archive.close().await.unwrap();
    let archive = Archive::open(ArchiveConfig::new(path)).unwrap();
    assert_eq!(archive.events(0, 100).await.unwrap().len(), 1);
    assert!(archive.get_sweep("byte-rejected").await.unwrap().is_none());
    archive.close().await.unwrap();
}

#[tokio::test]
async fn sql_failure_invalid_raw_and_cross_session_owners_rollback_the_entire_source_event_graph() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("archive.sqlite");
    let archive = Archive::open(ArchiveConfig::new(&path)).unwrap();
    archive.create_session(session()).await.unwrap();
    {
        let db = rusqlite::Connection::open(&path).unwrap();
        db.execute_batch("CREATE TRIGGER reject_second BEFORE INSERT ON sweeps WHEN NEW.id='bad' BEGIN SELECT RAISE(ABORT,'owned synthetic failure');END;").unwrap();
    }
    assert!(
        archive
            .append_sweeps(vec![source("good"), source("bad")])
            .await
            .is_err()
    );
    assert!(archive.get_sweep("good").await.unwrap().is_none());
    assert_eq!(archive.events(0, 100).await.unwrap().len(), 1);
    assert_eq!(
        archive
            .sweep_ingestion_boundary("fixture-сессия")
            .await
            .unwrap(),
        0
    );
    let mut invalid = source("not-numeric");
    invalid["levels"][0] = json!("-100");
    assert!(archive.append_sweeps(vec![invalid]).await.is_err());
    assert!(
        archive
            .apply(vec![Mutation::Sweeps(vec![
                fixture()["observation"].clone()
            ])])
            .await
            .is_err()
    );
    let mut foreign = session();
    foreign["id"] = json!("other-session");
    assert!(archive.create_session(foreign).await.is_err());
    assert!(
        archive
            .get_session("other-session")
            .await
            .unwrap()
            .is_none()
    );
    archive.close().await.unwrap();
}

#[tokio::test]
async fn only_clones_share_a_writer_and_close_drains_accepted_work_and_rejects_further_commands() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("archive.sqlite");
    let archive = Archive::open(ArchiveConfig::new(&path)).unwrap();
    assert!(matches!(
        Archive::open(ArchiveConfig::new(&path)),
        Err(ArchiveError::AlreadyOpen)
    ));
    archive.create_session(session()).await.unwrap();
    let clone = archive.clone();
    clone
        .append_sweeps(vec![source("durable-before-close")])
        .await
        .unwrap();
    let (a, b) = tokio::join!(archive.close(), clone.close());
    a.unwrap();
    b.unwrap();
    assert!(matches!(
        clone.append_sweeps(vec![source("after-close")]).await,
        Err(ArchiveError::Closed)
    ));
    let reopened = Archive::open(ArchiveConfig::new(&path)).unwrap();
    assert!(
        reopened
            .get_sweep("durable-before-close")
            .await
            .unwrap()
            .is_some()
    );
    assert!(reopened.get_sweep("after-close").await.unwrap().is_none());
    reopened.close().await.unwrap();
}
