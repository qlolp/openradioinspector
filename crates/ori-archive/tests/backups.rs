use ori_archive::{Archive, ArchiveConfig, Mutation, restore_backup, verify_backup};
use serde_json::json;
use std::{
    fs,
    io::{Seek, SeekFrom, Write},
    path::Path,
};
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

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn live_wal_recovery_is_sealed_consistent_private_and_retains_new_precision_and_all_audio_bytes()
 {
    let (dir, path) = legacy();
    let archive = Archive::open(ArchiveConfig::new(&path)).unwrap();
    let mut observation = archive
        .get_observation("fixture-observation-α")
        .await
        .unwrap()
        .unwrap();
    observation["id"] = json!("negative-zero");
    observation["level"] = json!(-0.0);
    archive
        .append_observations(vec![observation])
        .await
        .unwrap();
    assert!(
        fs::metadata(format!("{}-wal", path.display()))
            .unwrap()
            .len()
            > 0
    );
    let bundle = dir.path().join("recovery");
    let manifest = archive.backup(&bundle).await.unwrap();
    assert_eq!(manifest["backup_version"], 1);
    assert_eq!(manifest["purpose"], "private-recovery");
    assert_eq!(manifest["archive_schema"], 6);
    assert_eq!(manifest["counts"]["observations"], 2);
    assert_eq!(manifest["counts"]["artifacts"], 1);
    assert_eq!(verify_backup(&bundle).unwrap(), manifest);
    for suffix in ["-wal", "-shm", "-journal"] {
        assert!(
            !Path::new(&format!(
                "{}{suffix}",
                bundle.join("archive.sqlite").display()
            ))
            .exists()
        );
    }
    let restored = dir.path().join("restored.sqlite");
    restore_backup(&bundle, &restored).unwrap();
    let reopened = Archive::open(ArchiveConfig::new(&restored)).unwrap();
    let level = reopened
        .get_observation("negative-zero")
        .await
        .unwrap()
        .unwrap()["level"]
        .as_f64()
        .unwrap();
    assert_eq!(level.to_bits(), (-0.0_f64).to_bits());
    assert_eq!(
        reopened
            .get_artifact("fixture-сессия", "receiver+one", "synthetic-audio")
            .await
            .unwrap()
            .unwrap()
            .payload,
        archive
            .get_artifact("fixture-сессия", "receiver+one", "synthetic-audio")
            .await
            .unwrap()
            .unwrap()
            .payload
    );
    assert_eq!(
        reopened
            .monitoring("project", "synthetic-project")
            .await
            .unwrap()
            .unwrap()["autostart"],
        true
    );
    assert!(restore_backup(&bundle, &restored).is_err());
    assert!(archive.backup(&bundle).await.is_err());
    reopened.close().await.unwrap();
    archive.close().await.unwrap();
}

#[tokio::test]
async fn corrupt_bundle_or_closed_audio_checksum_never_publishes_a_restored_or_partial_archive() {
    let (dir, path) = legacy();
    let archive = Archive::open(ArchiveConfig::new(&path)).unwrap();
    let bundle = dir.path().join("recovery");
    archive.backup(&bundle).await.unwrap();
    let target = dir.path().join("should-not-exist.sqlite");
    let mut file = fs::OpenOptions::new()
        .write(true)
        .open(bundle.join("archive.sqlite"))
        .unwrap();
    file.seek(SeekFrom::Start(100)).unwrap();
    file.write_all(b"corrupted").unwrap();
    file.sync_all().unwrap();
    assert!(verify_backup(&bundle).is_err());
    assert!(restore_backup(&bundle, &target).is_err());
    assert!(!target.exists());
    {
        let db = rusqlite::Connection::open(&path).unwrap();
        db.execute(
            "UPDATE instrument_artifacts SET payload=? WHERE id='synthetic-audio'",
            [b"RIFF wrong payload".as_slice()],
        )
        .unwrap();
    }
    let rejected = dir.path().join("invalid-audio");
    assert!(archive.backup(&rejected).await.is_err());
    assert!(!rejected.exists());
    archive.close().await.unwrap();
}

#[tokio::test]
async fn portable_export_preserves_sources_and_precision_but_removes_private_credentials_and_resume_intent()
 {
    let (dir, path) = legacy();
    let archive = Archive::open(ArchiveConfig::new(&path)).unwrap();
    let exported = dir.path().join("portable.sqlite");
    archive.export_portable(&exported).await.unwrap();
    let original = archive.get_sweep("arrival-last").await.unwrap().unwrap();
    let opened = Archive::open(ArchiveConfig::new(&exported)).unwrap();
    assert_eq!(
        opened.get_sweep("arrival-last").await.unwrap().unwrap(),
        original
    );
    let project = opened
        .monitoring("project", "synthetic-project")
        .await
        .unwrap()
        .unwrap();
    assert_eq!(project["autostart"], false);
    assert_eq!(project["resume_requested"], false);
    assert_eq!(project["webhook_enabled"], false);
    assert_eq!(project["state"], "interrupted");
    let bytes = fs::read(&exported).unwrap();
    assert!(
        !bytes
            .windows("synthetic-fixture-secret".len())
            .any(|slice| slice == b"synthetic-fixture-secret")
    );
    let db = rusqlite::Connection::open(&exported).unwrap();
    assert_eq!(
        db.query_row("SELECT COUNT(*) FROM monitoring_webhooks", [], |r| r
            .get::<_, i64>(0))
            .unwrap(),
        0
    );
    assert_eq!(
        db.query_row("SELECT state FROM monitoring_notifications", [], |r| r
            .get::<_, String>(
            0
        ))
        .unwrap(),
        "disabled"
    );
    assert!(archive.export_portable(&exported).await.is_err());
    assert!(archive.export_portable(&path).await.is_err());
    opened.close().await.unwrap();
    archive.close().await.unwrap();
}

#[tokio::test]
async fn recovery_keeps_atomic_event_watermark_while_new_source_batches_commit() {
    let (dir, path) = legacy();
    let archive = Archive::open(ArchiveConfig::new(&path)).unwrap();
    let source = archive.get_sweep("arrival-last").await.unwrap().unwrap();
    let concurrent = archive.clone();
    let writer = tokio::spawn(async move {
        for batch in 0..8 {
            let values = (0..4)
                .map(|n| {
                    let mut value = source.clone();
                    value["id"] = json!(format!("concurrent-{batch}-{n}"));
                    value
                })
                .collect();
            concurrent.append_sweeps(values).await.unwrap();
        }
    });
    let bundle = dir.path().join("concurrent-backup");
    let manifest = archive.backup(&bundle).await.unwrap();
    writer.await.unwrap();
    let backup = rusqlite::Connection::open(bundle.join("archive.sqlite")).unwrap();
    let sources: i64 = backup
        .query_row("SELECT COUNT(*) FROM sweeps", [], |r| r.get(0))
        .unwrap();
    let events: i64 = backup
        .query_row(
            "SELECT COUNT(*) FROM events WHERE event_type='sweep.received'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(sources, events + 4);
    assert_eq!(manifest["event_watermark"].as_i64().unwrap(), events);
    assert_eq!(events % 4, 0);
    archive.close().await.unwrap();
}

#[tokio::test]
async fn restore_rejects_existing_sidecars_and_backup_preserves_monitoring_payloads_without_secret_echo()
 {
    let (dir, path) = legacy();
    let archive = Archive::open(ArchiveConfig::new(&path)).unwrap();
    let bundle = dir.path().join("recovery");
    archive.backup(&bundle).await.unwrap();
    let target = dir.path().join("restored.sqlite");
    fs::write(
        format!("{}-wal", target.display()),
        b"owned synthetic stale sidecar",
    )
    .unwrap();
    assert!(restore_backup(&bundle, &target).is_err());
    assert!(!target.exists());
    assert_eq!(
        archive.webhook_status("synthetic-project").await.unwrap(),
        json!({"configured":true,"url":"https://example.invalid/owned-fixture"})
    );
    archive.apply(vec![Mutation::Monitoring{kind:"quality".into(),value:json!({"id":"quality","project_id":"synthetic-project","created_at":"2026-11-11T12:00:00Z","state":"readable"})}]).await.unwrap();
    archive.close().await.unwrap();
}
