#[test]
fn build_info_is_read_only_and_identifies_the_compiled_supervisor() {
    let directory = tempfile::tempdir().unwrap();
    let result = std::process::Command::new(env!("CARGO_BIN_EXE_ori-supervisor"))
        .arg("--build-info")
        .current_dir(directory.path())
        .env(
            "ORI_RUNTIME_DATA_DIR",
            directory.path().join("must-not-exist"),
        )
        .output()
        .unwrap();
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    let value: serde_json::Value = serde_json::from_slice(&result.stdout).unwrap();
    assert_eq!(value["component"], "ori-supervisor");
    assert_eq!(value["schema_version"], 1);
    assert_eq!(value["target_arch"], std::env::consts::ARCH);
    assert_eq!(value["target_os"], std::env::consts::OS);
    assert_eq!(value["version"], env!("CARGO_PKG_VERSION"));
    assert_eq!(
        value["source_revision"],
        serde_json::json!(option_env!("ORI_BUILD_SOURCE_SHA"))
    );
    assert_eq!(std::fs::read_dir(directory.path()).unwrap().count(), 0);
}
