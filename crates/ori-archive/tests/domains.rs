use ori_archive::{Archive, ArchiveConfig, Artifact, EventDraft, Mutation};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
fn legacy() -> (tempfile::TempDir, std::path::PathBuf) {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("archive.sqlite");
    std::fs::copy(
        concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/tests/fixtures/legacy_v5_utf16.sqlite"
        ),
        &path,
    )
    .unwrap();
    (dir, path)
}
fn candidate() -> Value {
    json!({"id":"candidate-one","session_id":"fixture-сессия","source_stream_id":"spectrum/one","interval":{"started_at":"2026-11-11T12:00:01Z","ended_at":"2026-11-11T12:00:02Z"},"center_frequency_hz":100000003,"level_unit":"dBm","state":"investigating"})
}
fn investigation(id: &str, state: &str) -> Value {
    json!({"id":id,"candidate_id":"candidate-one","target_stream_id":"receiver+one","target_frequency_hz":100000003,"state":state,"priority":0,"created_at":"2026-11-11T12:00:03Z"})
}

#[tokio::test]
async fn recovery_can_atomically_reset_streams_and_interrupt_active_work_without_replaying_queued_jobs()
 {
    let (_dir, path) = legacy();
    let archive = Archive::open(ArchiveConfig::new(path)).unwrap();
    let mut session = archive
        .get_session("fixture-сессия")
        .await
        .unwrap()
        .unwrap();
    for stream in session["streams"].as_array_mut().unwrap() {
        stream["state"] = json!("running");
    }
    archive.save_session(session, None).await.unwrap();
    archive
        .apply(vec![
            Mutation::Candidate {
                value: candidate(),
                event: Some(EventDraft::now(
                    "candidate.created",
                    Some("fixture-сессия".into()),
                    json!({}),
                )),
            },
            Mutation::Investigation {
                session_id: "fixture-сессия".into(),
                value: investigation("active", "listening"),
                candidate: candidate(),
                event: Some(EventDraft::now(
                    "investigation.listening",
                    Some("fixture-сессия".into()),
                    json!({}),
                )),
            },
            Mutation::Investigation {
                session_id: "fixture-сессия".into(),
                value: investigation("queued", "queued"),
                candidate: candidate(),
                event: None,
            },
        ])
        .await
        .unwrap();
    let open = archive.list_open_investigations(32, None).await.unwrap();
    assert_eq!(open.len(), 2);
    assert!(open.iter().all(|(sid, _)| sid == "fixture-сессия"));
    let mut active = archive
        .get_investigation("active")
        .await
        .unwrap()
        .unwrap()
        .1;
    active["state"] = json!("failed");
    active["ended_at"] = json!("2026-11-11T12:00:10Z");
    active["error"] = json!("Synthetic service interruption");
    let mut source = archive
        .get_candidate("candidate-one")
        .await
        .unwrap()
        .unwrap();
    source["state"] = json!("queued");
    let mut mutations = archive.runtime_reset_mutations();
    mutations.push(Mutation::Investigation {
        session_id: "fixture-сессия".into(),
        value: active,
        candidate: source,
        event: Some(EventDraft::now(
            "investigation.interrupted",
            Some("fixture-сессия".into()),
            json!({"synthetic":true}),
        )),
    });
    let committed = archive.apply(mutations).await.unwrap();
    assert_eq!(committed.events.len(), 1);
    assert_eq!(committed.events[0]["sequence"], 3);
    let session = archive
        .get_session("fixture-сессия")
        .await
        .unwrap()
        .unwrap();
    assert!(
        session["streams"]
            .as_array()
            .unwrap()
            .iter()
            .all(|s| s["state"] == "disconnected")
    );
    assert_eq!(
        archive
            .get_investigation("active")
            .await
            .unwrap()
            .unwrap()
            .1["state"],
        "failed"
    );
    assert_eq!(
        archive
            .get_investigation("queued")
            .await
            .unwrap()
            .unwrap()
            .1["state"],
        "queued"
    );
    assert_eq!(
        archive
            .get_candidate("candidate-one")
            .await
            .unwrap()
            .unwrap()["state"],
        "queued"
    );
    assert_eq!(
        archive
            .list_candidates("fixture-сессия", Some("queued"), 10, None)
            .await
            .unwrap()
            .items
            .len(),
        1
    );
    assert_eq!(
        archive
            .list_investigations(Some("fixture-сессия"), Some("failed"), 10, None)
            .await
            .unwrap()
            .items
            .len(),
        1
    );
    archive.close().await.unwrap();
}

#[tokio::test]
async fn named_references_are_idempotent_and_measurement_results_remain_separate_from_sources() {
    let (_dir, path) = legacy();
    let archive = Archive::open(ArchiveConfig::new(path)).unwrap();
    let reference = json!({"id":"reference-one","session_id":"fixture-сессия","stream_id":"spectrum/one","sweep_id":"arrival-last","name":"Quiet","created_at":"2026-11-11T12:10:00Z","interval":{"started_at":"2026-11-11T12:00:00.123456Z","ended_at":"2026-11-11T12:00:00.223456Z"},"level_unit":"dBm","points":4});
    let first = archive
        .save_reference(
            reference.clone(),
            EventDraft::now(
                "sweep.reference_saved",
                Some("fixture-сессия".into()),
                json!({}),
            ),
        )
        .await
        .unwrap();
    assert_eq!(first.events.len(), 1);
    let mut duplicate = reference.clone();
    duplicate["id"] = json!("should-not-be-inserted");
    let same = archive
        .save_reference(
            duplicate,
            EventDraft::now(
                "sweep.reference_saved",
                Some("fixture-сессия".into()),
                json!({}),
            ),
        )
        .await
        .unwrap();
    assert!(same.events.is_empty());
    assert_eq!(same.items[0]["id"], "reference-one");
    let mut conflict = reference;
    conflict["sweep_id"] = json!("arrival-first");
    assert!(
        archive
            .save_reference(conflict, EventDraft::now("ignored", None, json!({})))
            .await
            .is_err()
    );
    assert_eq!(
        archive
            .list_references("fixture-сессия", None, 100)
            .await
            .unwrap()
            .len(),
        1
    );
    let result = json!({"id":"result-one","session_id":"fixture-сессия","stream_id":"spectrum/one","methodology":"signal_to_noise","result_code":"norm","frequency_hz":100000003,"interval":{"started_at":"2026-11-11T12:00:00Z","ended_at":"2026-11-11T12:00:01Z"},"values":{"sn_db":12.5},"created_at":"2026-11-11T12:10:00Z"});
    archive.save_measurement_result(result, None).await.unwrap();
    let values = archive
        .list_measurement_results("fixture-сессия", Some("signal_to_noise"), 10, None)
        .await
        .unwrap();
    assert_eq!(values.items.len(), 1);
    assert_eq!(values.items[0]["values"]["sn_db"], 12.5);
    assert_eq!(
        archive
            .list_sweeps("fixture-сессия", None, 100)
            .await
            .unwrap()
            .len(),
        4
    );
    archive.close().await.unwrap();
}

#[tokio::test]
async fn closed_audio_bytes_hash_and_artifact_scope_are_exact_and_wrong_checksums_never_commit() {
    let (_dir, path) = legacy();
    let archive = Archive::open(ArchiveConfig::new(path)).unwrap();
    let original = archive
        .get_artifact("fixture-сессия", "receiver+one", "synthetic-audio")
        .await
        .unwrap()
        .unwrap();
    assert!(original.payload.starts_with(b"RIFF"));
    let mut value = Artifact {
        id: "copied-audio".into(),
        ..original.clone()
    };
    value.sha256 = Sha256::digest(&value.payload)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    archive.put_artifact(value.clone()).await.unwrap();
    let loaded = archive
        .get_artifact("fixture-сессия", "receiver+one", "copied-audio")
        .await
        .unwrap()
        .unwrap();
    assert_eq!(loaded.payload, original.payload);
    assert_eq!(loaded.sha256, original.sha256);
    assert!(
        archive
            .get_artifact("fixture-сессия", "spectrum/one", "copied-audio")
            .await
            .unwrap()
            .is_none()
    );
    assert_eq!(
        archive
            .list_artifacts("fixture-сессия", "receiver+one", 10, 0)
            .await
            .unwrap()
            .len(),
        2
    );
    value.id = "bad-hash".into();
    value.sha256 = "0".repeat(64);
    assert!(archive.put_artifact(value).await.is_err());
    assert!(
        archive
            .get_artifact("fixture-сессия", "receiver+one", "bad-hash")
            .await
            .unwrap()
            .is_none()
    );
    archive.close().await.unwrap();
}

#[tokio::test]
async fn binary_payload_queue_accounting_counts_bytes_without_json_array_expansion() {
    let (_dir, path) = legacy();
    let archive = Archive::open(ArchiveConfig::new(path)).unwrap();
    let payload = vec![255; 12 * 1024 * 1024];
    let digest = Sha256::digest(&payload)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    let item = Artifact {
        id: "large-synthetic".into(),
        session_id: "fixture-сессия".into(),
        stream_id: "receiver+one".into(),
        schema_version: 1,
        kind: "diagnostic".into(),
        mime_type: "application/octet-stream".into(),
        filename: "synthetic.bin".into(),
        sha256: digest,
        created_at: "2026-11-11T12:00:01Z".into(),
        payload,
        metadata: json!({"synthetic":true}),
    };
    let result = archive.put_artifact(item).await.unwrap();
    let retained = archive.queue_status().retained_bytes;
    assert!((12 * 1024 * 1024..13 * 1024 * 1024).contains(&retained));
    drop(result);
    assert_eq!(archive.queue_status().retained_bytes, 0);
    assert_eq!(
        archive
            .get_artifact("fixture-сессия", "receiver+one", "large-synthetic")
            .await
            .unwrap()
            .unwrap()
            .payload
            .len(),
        12 * 1024 * 1024
    );
    archive.close().await.unwrap();
}
