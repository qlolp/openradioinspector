use ori_archive::{
    Archive, ArchiveConfig, ArchiveError, BackupCancellation, BackupOptions, restore_backup,
    verify_backup,
};
use serde_json::{Value, json};
use std::time::Duration;

fn fixture() -> Value {
    ori_archive::parse_json_value(
        include_bytes!("../../../contracts/fixtures/measurement-v1.json"),
        1024 * 1024,
    )
    .unwrap()
}
fn options(bytes: u64) -> BackupOptions {
    BackupOptions {
        max_snapshot_bytes: bytes,
        copy_timeout: Duration::from_secs(2),
        total_timeout: Duration::from_secs(5),
        cancel: BackupCancellation::default(),
    }
}
async fn seeded(timeout: Duration) -> (tempfile::TempDir, Archive) {
    let root = tempfile::tempdir().unwrap();
    let mut config = ArchiveConfig::new(root.path().join("archive.sqlite"));
    config.backup_timeout = timeout;
    let archive = Archive::open(config).unwrap();
    let mut session = fixture()["session"].clone();
    session["created_at"] = json!("2026-11-11T12:00:00.123456Z");
    archive.create_session(session).await.unwrap();
    archive
        .append_sweeps(vec![fixture()["sweep"].clone()])
        .await
        .unwrap();
    archive
        .append_observations(vec![fixture()["observation"].clone()])
        .await
        .unwrap();
    (root, archive)
}

#[tokio::test]
async fn explicit_backup_policy_preserves_all_sources_events_and_legacy_copy_deadline() {
    let (root, archive) = seeded(Duration::from_nanos(1)).await;
    let expected = archive
        .get_sweep(fixture()["sweep"]["id"].as_str().unwrap())
        .await
        .unwrap()
        .unwrap();
    assert!(matches!(
        archive.backup(&root.path().join("legacy-short")).await,
        Err(ArchiveError::QueryBudget)
    ));
    let bytes = archive.capacity().await.unwrap()["database_bytes"]
        .as_u64()
        .unwrap();
    let target = root.path().join("limited");
    let manifest = archive
        .backup_with_options(&target, options(bytes))
        .await
        .unwrap();
    assert_eq!(
        manifest["counts"],
        json!({"sweeps":1,"observations":1,"events":3,"artifacts":0})
    );
    assert_eq!(manifest["archive"]["bytes"], bytes);
    let verified = target.clone();
    assert_eq!(
        tokio::task::spawn_blocking(move || verify_backup(&verified))
            .await
            .unwrap()
            .unwrap(),
        manifest
    );
    let destination = root.path().join("restored.sqlite");
    let restored_path = destination.clone();
    tokio::task::spawn_blocking(move || restore_backup(&target, &restored_path))
        .await
        .unwrap()
        .unwrap();
    let restored = Archive::open(ArchiveConfig::new(destination)).unwrap();
    let actual = restored
        .get_sweep(expected["id"].as_str().unwrap())
        .await
        .unwrap()
        .unwrap();
    assert_eq!(actual["id"], expected["id"]);
    assert_eq!(actual["interval"], expected["interval"]);
    assert_eq!(actual["frequencies_hz"], expected["frequencies_hz"]);
    assert_eq!(actual["settings"], expected["settings"]);
    for (a, b) in actual["levels"]
        .as_array()
        .unwrap()
        .iter()
        .zip(expected["levels"].as_array().unwrap())
    {
        assert_eq!(a.as_f64().unwrap().to_bits(), b.as_f64().unwrap().to_bits());
    }
    assert_eq!(
        restored.events(0, 100).await.unwrap(),
        archive.events(0, 100).await.unwrap()
    );
    restored.close().await.unwrap();
    assert!(matches!(
        archive
            .backup(&root.path().join("legacy-still-short"))
            .await,
        Err(ArchiveError::QueryBudget)
    ));
    archive.close().await.unwrap();
}

#[tokio::test]
async fn source_quota_and_mathematical_copy_floor_reject_before_creating_parent_or_stage() {
    let (root, archive) = seeded(Duration::from_secs(30)).await;
    let bytes = archive.capacity().await.unwrap()["database_bytes"]
        .as_u64()
        .unwrap();
    let baseline = archive.events(0, 100).await.unwrap();
    let destination = root.path().join("not-created/bundle");
    assert!(matches!(
        archive
            .backup_with_options(&destination, options(bytes - 1))
            .await,
        Err(ArchiveError::QueryBudget)
    ));
    let mut short = options(bytes);
    short.copy_timeout = Duration::from_nanos(1);
    assert!(matches!(
        archive.backup_with_options(&destination, short).await,
        Err(ArchiveError::QueryBudget)
    ));
    assert!(
        !destination.parent().unwrap().exists(),
        "inadmissible work must not create staging paths"
    );
    assert_eq!(archive.events(0, 100).await.unwrap(), baseline);
    archive.close().await.unwrap();
}

#[tokio::test]
async fn invalid_or_unrepresentable_policy_is_an_error_without_io_or_panic() {
    let (root, archive) = seeded(Duration::from_secs(30)).await;
    for case in 0..5 {
        let mut policy = options(1024 * 1024);
        match case {
            0 => policy.max_snapshot_bytes = 0,
            1 => policy.copy_timeout = Duration::ZERO,
            2 => policy.total_timeout = Duration::ZERO,
            3 => policy.total_timeout = Duration::from_millis(1),
            4 => {
                policy.copy_timeout = Duration::MAX;
                policy.total_timeout = Duration::MAX;
            }
            _ => unreachable!(),
        }
        assert!(matches!(
            archive
                .backup_with_options(&root.path().join("invalid/bundle"), policy)
                .await,
            Err(ArchiveError::Invalid(_))
        ));
        assert!(!root.path().join("invalid").exists());
    }
    archive.close().await.unwrap();
}

#[tokio::test]
async fn explicit_cancellation_is_shared_and_fail_closed_before_copy_without_poisoning_archive() {
    let (root, archive) = seeded(Duration::from_secs(30)).await;
    let bytes = archive.capacity().await.unwrap()["database_bytes"]
        .as_u64()
        .unwrap();
    let policy = options(bytes);
    let control = policy.cancel.clone();
    control.cancel();
    assert!(control.is_cancelled());
    assert!(policy.cancel.is_cancelled());
    assert!(matches!(
        archive
            .backup_with_options(&root.path().join("cancelled/bundle"), policy)
            .await,
        Err(ArchiveError::Closed)
    ));
    assert!(!root.path().join("cancelled").exists());
    archive
        .backup_with_options(&root.path().join("fresh"), options(bytes))
        .await
        .unwrap();
    archive.close().await.unwrap();
}

#[tokio::test]
async fn response_budget_rejects_before_publishing_an_unacknowledged_recovery_bundle() {
    let root = tempfile::tempdir().unwrap();
    let mut config = ArchiveConfig::new(root.path().join("archive.sqlite"));
    config.max_response_bytes = 1;
    let archive = Archive::open(config).unwrap();
    let target = root.path().join("not-published");
    assert!(matches!(
        archive
            .backup_with_options(&target, options(1024 * 1024))
            .await,
        Err(ArchiveError::QueryBudget)
    ));
    assert!(!target.exists());
    assert!(fs_no_stages(root.path()));
    archive.close().await.unwrap();
}

fn fs_no_stages(root: &std::path::Path) -> bool {
    std::fs::read_dir(root).unwrap().all(|entry| {
        !entry
            .unwrap()
            .file_name()
            .to_string_lossy()
            .starts_with(".ori-recovery-")
    })
}
