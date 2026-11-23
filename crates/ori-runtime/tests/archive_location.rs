use ori_runtime::lease::WriterLease;
use serde_json::Value;
use std::{
    fs,
    path::Path,
    process::{Command, Output},
};

fn relocate(data: &Path, target: &Path) -> Output {
    let data = data.canonicalize().unwrap();
    let target = target
        .parent()
        .unwrap()
        .canonicalize()
        .unwrap()
        .join(target.file_name().unwrap());
    Command::new(env!("CARGO_BIN_EXE_ori-supervisor"))
        .arg("archive-location")
        .arg("--data-dir")
        .arg(data)
        .arg("--directory")
        .arg(target)
        .output()
        .unwrap()
}

#[test]
fn selects_a_new_archive_only_while_stopped_and_refuses_to_overwrite() {
    let data = tempfile::tempdir().unwrap();
    let disk = tempfile::tempdir().unwrap();
    let target = disk.path().join("measurements");
    let busy = WriterLease::acquire(&data.path().canonicalize().unwrap()).unwrap();
    assert!(!relocate(data.path(), &target).status.success());
    assert!(!target.exists());
    drop(busy);
    let output = relocate(data.path(), &target);
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(
        serde_json::from_slice::<Value>(&output.stdout).unwrap()["database"],
        "sessions.sqlite3"
    );
    assert!(target.join("sessions.sqlite3").is_file());
    assert!(relocate(data.path(), &target).status.success());
    let occupied = disk.path().join("occupied");
    fs::create_dir(&occupied).unwrap();
    fs::write(
        occupied.join("sessions.sqlite3"),
        b"owned by another installation",
    )
    .unwrap();
    assert!(!relocate(data.path(), &occupied).status.success());
    assert_eq!(
        fs::read(occupied.join("sessions.sqlite3")).unwrap(),
        b"owned by another installation"
    );
    let second = disk.path().join("second");
    let ok = relocate(data.path(), &second);
    assert!(
        ok.status.success(),
        "{}",
        String::from_utf8_lossy(&ok.stderr)
    );
    assert!(target.join("sessions.sqlite3").is_file());
    assert!(second.join("sessions.sqlite3").is_file());
}

#[test]
fn lost_database_with_existing_wal_requires_recovery_before_relocation() {
    let node = tempfile::tempdir().unwrap();
    let disk = tempfile::tempdir().unwrap();
    fs::write(node.path().join("sessions.sqlite3-wal"), b"retained WAL").unwrap();
    let target = disk.path().join("measurements");
    assert!(!relocate(node.path(), &target).status.success());
    assert!(!target.join("sessions.sqlite3").exists());
    assert!(
        !node
            .path()
            .join(".ori-runtime/archive-location.json")
            .exists()
    );
}

#[tokio::test]
async fn moving_a_stopped_archive_preserves_measurements_and_closed_audio() {
    use ori_archive::{Archive, ArchiveConfig};
    let node = tempfile::tempdir().unwrap();
    let disk = tempfile::tempdir().unwrap();
    let source = node.path().join("sessions.sqlite3");
    fs::copy(
        concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../ori-archive/tests/fixtures/legacy_v5_utf16.sqlite"
        ),
        &source,
    )
    .unwrap();
    let archive = Archive::open(ArchiveConfig::new(&source)).unwrap();
    let sweep = archive.get_sweep("arrival-last").await.unwrap().unwrap();
    let mut observation = archive
        .get_observation("fixture-observation-α")
        .await
        .unwrap()
        .unwrap();
    observation["id"] = serde_json::json!("relocation-precision");
    observation["level"] = serde_json::json!(-0.0);
    archive
        .append_observations(vec![observation.clone()])
        .await
        .unwrap();
    let audio = archive
        .get_artifact("fixture-сессия", "receiver+one", "synthetic-audio")
        .await
        .unwrap()
        .unwrap()
        .payload;
    archive.close().await.unwrap();
    let original = fs::read(&source).unwrap();
    let target = disk.path().join("measurements");
    let moved = relocate(node.path(), &target);
    assert!(
        moved.status.success(),
        "{}",
        String::from_utf8_lossy(&moved.stderr)
    );
    assert_eq!(fs::read(source).unwrap(), original);
    let selected =
        ori_runtime::archive_location::directory(&node.path().canonicalize().unwrap()).unwrap();
    assert_eq!(selected, target.canonicalize().unwrap());
    let restored = Archive::open(ArchiveConfig::new(selected.join("sessions.sqlite3"))).unwrap();
    assert_eq!(
        restored.get_sweep("arrival-last").await.unwrap().unwrap(),
        sweep
    );
    let recovered = restored
        .get_observation("relocation-precision")
        .await
        .unwrap()
        .unwrap();
    assert_eq!(recovered, observation);
    assert_eq!(
        recovered["level"].as_f64().unwrap().to_bits(),
        (-0.0_f64).to_bits()
    );
    assert_eq!(
        restored
            .get_artifact("fixture-сессия", "receiver+one", "synthetic-audio")
            .await
            .unwrap()
            .unwrap()
            .payload,
        audio
    );
    restored.close().await.unwrap();
}
