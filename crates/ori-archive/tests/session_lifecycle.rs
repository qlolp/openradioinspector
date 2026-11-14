use ori_archive::{Archive, ArchiveConfig, ArchiveError, Mutation};
use serde_json::{Value, json};
use std::time::Duration;

fn session(id: &str, state: &str) -> Value {
    json!({"id":id,"name":"Synthetic lifecycle receipt","state":state,
        "created_at":"2026-11-11T00:00:00.123456Z",
        "started_at":if state=="running" {Some("2026-11-11T00:00:01.123456Z")}else{None},
        "streams":[
            {"id":format!("{id}-analyzer"),"device":"synthetic","role":"spectrum_analyzer","level_unit":"dBm"},
            {"id":format!("{id}-receiver"),"device":"synthetic","role":"receiver","level_unit":"s_meter_count"}]})
}
fn open() -> (tempfile::TempDir, Archive) {
    let root = tempfile::tempdir().unwrap();
    let archive = Archive::open(ArchiveConfig::new(root.path().join("source.sqlite"))).unwrap();
    (root, archive)
}

#[tokio::test]
async fn draft_enrollment_is_not_running_authority_and_fresh_dispatch_never_refreshes_old_receipt()
{
    let (_root, archive) = open();
    assert!(archive.cached_session_lifecycle("missing").is_none());
    assert!(matches!(
        archive.capture_session_lifecycle("missing").await,
        Err(ArchiveError::NotFound)
    ));
    archive.create_session(session("é", "draft")).await.unwrap();
    let enrolled = archive.capture_session_lifecycle("é").await.unwrap();
    assert!(!enrolled.check());
    assert!(archive.cached_session_lifecycle("é").is_none());
    let mut running = archive.get_session("é").await.unwrap().unwrap();
    running["state"] = json!("running");
    running["started_at"] = json!("2026-11-11T00:00:01.123456Z");
    archive.save_session(running, None).await.unwrap();
    let dispatched = archive.cached_session_lifecycle("é").unwrap();
    assert!(dispatched.check());
    assert!(
        !enrolled.check(),
        "Draft enrollment was refreshed into a running receipt"
    );
    assert!(archive.cached_session_lifecycle("e\u{301}").is_none());
    archive.close().await.unwrap();
    assert!(!dispatched.check());
}

#[tokio::test]
async fn capture_can_be_ordered_while_the_shared_domain_coordinator_is_already_held() {
    let (_root, archive) = open();
    archive
        .create_session(session("scope", "running"))
        .await
        .unwrap();
    let coordinator = archive.domain_coordinator();
    let _guard = coordinator.lock().await;
    let receipt = tokio::time::timeout(
        Duration::from_secs(5),
        archive.capture_session_lifecycle("scope"),
    )
    .await
    .expect("capture relocked the caller's domain coordinator")
    .unwrap();
    assert!(receipt.check());
    archive.close().await.unwrap();
    assert!(!receipt.check());
}

#[tokio::test]
async fn metadata_stream_raw_and_other_session_commits_do_not_revoke_running_receipts() {
    let (_root, archive) = open();
    archive
        .create_session(session("one", "running"))
        .await
        .unwrap();
    archive
        .create_session(session("two", "running"))
        .await
        .unwrap();
    let one = archive.capture_session_lifecycle("one").await.unwrap();
    let two = archive.capture_session_lifecycle("two").await.unwrap();
    let mut changed = archive.get_session("one").await.unwrap().unwrap();
    changed["name"] = json!("New label; same lifecycle");
    changed["metadata"] = json!({"é":-0.0,"e\u{301}":-1.0000000000000002});
    changed["streams"][0]["settings"] = json!({"rbw_hz":10000});
    archive.save_session(changed, None).await.unwrap();
    archive
        .apply(vec![Mutation::ResetRuntimeStreamStates])
        .await
        .unwrap();
    let raw = json!({"id":"raw-original","session_id":"one","stream_id":"one-analyzer",
        "interval":{"started_at":"2026-11-11T00:00:02.123456Z","ended_at":"2026-11-11T00:00:02.223456Z"},
        "frequencies_hz":[100000001,100000002],"levels":[-0.0,-1.0000000000000002],
        "level_unit":"dBm","settings":{"synthetic":true}});
    archive.append_sweeps(vec![raw.clone()]).await.unwrap();
    assert!(one.check() && two.check());
    let current = archive.get_session("two").await.unwrap().unwrap();
    archive
        .stop_session(current, "2026-11-11T00:00:03.123456Z")
        .await
        .unwrap();
    assert!(
        one.check(),
        "an unrelated session's stop revoked this receipt"
    );
    assert!(!two.check());
    assert_eq!(
        archive.get_sweep("raw-original").await.unwrap().unwrap(),
        raw
    );
    archive.close().await.unwrap();
}

#[tokio::test]
async fn committed_shared_archive_stop_cancels_jobs_atomically_and_receipts_cannot_resurrect() {
    let (_root, archive) = open();
    archive
        .create_session(session("scope", "running"))
        .await
        .unwrap();
    let original = archive.get_session("scope").await.unwrap().unwrap();
    let receipt = archive.capture_session_lifecycle("scope").await.unwrap();
    let clone = receipt.clone();
    archive.apply(vec![Mutation::Investigation {
        session_id:"scope".into(),
        candidate:json!({"id":"candidate","session_id":"scope","source_stream_id":"scope-analyzer",
            "interval":{"started_at":"2026-11-11T00:00:02Z","ended_at":"2026-11-11T00:00:03Z"},
            "center_frequency_hz":100000001,"level_unit":"dBm","state":"investigating"}),
        value:json!({"id":"job","candidate_id":"candidate","target_stream_id":"scope-receiver",
            "target_frequency_hz":100000001,"state":"queued","created_at":"2026-11-11T00:00:04Z"}),
        event:None,
    }]).await.unwrap();
    assert!(receipt.check());
    let stopped = archive
        .stop_session(original.clone(), "2026-11-11T00:00:05Z")
        .await
        .unwrap();
    assert_eq!(
        stopped
            .events
            .iter()
            .map(|event| event["event_type"].as_str().unwrap())
            .collect::<Vec<_>>(),
        ["session.stopped", "investigation.cancelled"]
    );
    assert_eq!(
        archive.get_investigation("job").await.unwrap().unwrap().1["state"],
        "cancelled"
    );
    assert!(!receipt.check() && !clone.check());
    assert!(archive.cached_session_lifecycle("scope").is_none());
    archive.save_session(original, None).await.unwrap();
    let current = archive.cached_session_lifecycle("scope").unwrap();
    assert!(current.check());
    assert!(
        !receipt.check() && !clone.check(),
        "stop/start resurrected the original receipt"
    );
    archive.close().await.unwrap();
}

#[tokio::test]
async fn rollback_after_a_real_stop_mutation_does_not_publish_a_lifecycle_change() {
    let (_root, archive) = open();
    archive
        .create_session(session("scope", "running"))
        .await
        .unwrap();
    let receipt = archive.capture_session_lifecycle("scope").await.unwrap();
    let original = archive.get_session("scope").await.unwrap().unwrap();
    let before = archive.events(0, 100).await.unwrap();
    let result = archive
        .apply(vec![
            Mutation::StopSession {
                value: original.clone(),
                at: "2026-11-11T00:00:05Z".into(),
            },
            Mutation::Sweeps(vec![
                json!({"id":"rejected","session_id":"scope","stream_id":"not-owned",
            "interval":{"started_at":"2026-11-11T00:00:02Z","ended_at":"2026-11-11T00:00:03Z"},
            "frequencies_hz":[100000001],"levels":[-0.0],"level_unit":"dBm","settings":{}}),
            ]),
        ])
        .await;
    assert!(matches!(result, Err(ArchiveError::Conflict)));
    assert!(receipt.check());
    assert!(archive.cached_session_lifecycle("scope").unwrap().check());
    assert_eq!(
        archive.get_session("scope").await.unwrap().unwrap(),
        original
    );
    assert_eq!(archive.events(0, 100).await.unwrap(), before);
    archive.close().await.unwrap();
}

#[tokio::test]
async fn same_transaction_stop_start_and_session_identity_changes_never_restore_old_authority() {
    let (_root, archive) = open();
    archive
        .create_session(session("scope", "running"))
        .await
        .unwrap();
    let original = archive.get_session("scope").await.unwrap().unwrap();
    let receipt = archive.capture_session_lifecycle("scope").await.unwrap();
    archive
        .apply(vec![
            Mutation::StopSession {
                value: original.clone(),
                at: "2026-11-11T00:00:05Z".into(),
            },
            Mutation::SaveSession {
                value: original.clone(),
                event: None,
            },
        ])
        .await
        .unwrap();
    assert!(
        !receipt.check(),
        "committed stop/start reused the original generation"
    );
    let next = archive.cached_session_lifecycle("scope").unwrap();
    let mut reused = original.clone();
    reused["created_at"] = json!("2026-11-11T00:00:00.123457Z");
    archive.save_session(reused, None).await.unwrap();
    assert!(!next.check());
    archive.save_session(original, None).await.unwrap();
    assert!(!receipt.check() && !next.check());
    archive.close().await.unwrap();
}

#[tokio::test]
async fn closed_archive_and_new_instance_with_same_ids_cannot_revive_a_receipt() {
    let (root, archive) = open();
    archive
        .create_session(session("scope", "running"))
        .await
        .unwrap();
    let receipt = archive.capture_session_lifecycle("scope").await.unwrap();
    archive.close().await.unwrap();
    assert!(!receipt.check());
    assert!(archive.cached_session_lifecycle("scope").is_none());
    assert!(matches!(
        archive.capture_session_lifecycle("scope").await,
        Err(ArchiveError::Closed)
    ));
    let reopened = Archive::open(ArchiveConfig::new(root.path().join("source.sqlite"))).unwrap();
    let current = reopened.capture_session_lifecycle("scope").await.unwrap();
    assert!(current.check());
    assert!(!receipt.check());
    reopened.close().await.unwrap();
}
