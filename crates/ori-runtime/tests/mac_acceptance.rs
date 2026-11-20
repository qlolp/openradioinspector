#![cfg(unix)]
use ori_runtime::macos_agent::{acceptance_configuration, configuration};
use serde_json::json;
use std::{fs, os::unix::fs::PermissionsExt};

#[test]
fn acceptance_service_is_explicit_separate_and_cannot_select_another_users_profile() {
    let root = tempfile::tempdir().unwrap();
    let home = root.path().join("home");
    let base = home.join("Library/Application Support/OpenRadioInspector/macOS-Acceptance");
    fs::create_dir_all(&base).unwrap();
    fs::set_permissions(&base, fs::Permissions::from_mode(0o700)).unwrap();
    let data = root.path().join("test-data");
    fs::create_dir(&data).unwrap();
    fs::set_permissions(&data, fs::Permissions::from_mode(0o700)).unwrap();
    let profile = root.path().join("core-profile.json");
    fs::write(
        &profile,
        serde_json::to_vec(&json!({"version":1,"run_id":"bound-run","phase":"hardware",
        "data_dir":data,"output_dir":root.path(),"expected_core_sha256":"a".repeat(64)}))
        .unwrap(),
    )
    .unwrap();
    fs::set_permissions(&profile, fs::Permissions::from_mode(0o600)).unwrap();
    let active = base.join("active.json");
    fs::write(
        &active,
        serde_json::to_vec(&json!({"version":1,"run_id":"bound-run","profile":profile})).unwrap(),
    )
    .unwrap();
    fs::set_permissions(&active, fs::Permissions::from_mode(0o600)).unwrap();
    let bundle = root.path().join("Candidate.app");
    let config = acceptance_configuration(&bundle, &home).unwrap();
    assert_eq!(config.data_directory, data);
    assert_eq!(config.backend.control_addr.to_string(), "127.0.0.1:8774");
    assert!(
        config
            .backend
            .args
            .windows(2)
            .any(|p| p[0] == "--acceptance-profile" && p[1] == profile.to_str().unwrap())
    );
    assert_eq!(
        configuration(&bundle, &data, 1024)
            .unwrap()
            .backend
            .control_addr
            .to_string(),
        "127.0.0.1:8772"
    );
    fs::set_permissions(&profile, fs::Permissions::from_mode(0o644)).unwrap();
    assert!(acceptance_configuration(&bundle, &home).is_err());
}
