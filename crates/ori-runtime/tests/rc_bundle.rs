use ori_runtime::macos_agent::bundled_backend_id;
use sha2::{Digest, Sha256};
#[test]
fn bundle_identity_binds_helpers_and_refuses_changed_or_mixed_components() {
    let temp = tempfile::tempdir().unwrap();
    let app = temp.path().join("RC.app");
    std::fs::create_dir_all(app.join("Contents/Helpers")).unwrap();
    std::fs::create_dir_all(app.join("Contents/Resources")).unwrap();
    let mut components = serde_json::Map::new();
    for name in ["ori-core", "ori-supervisor"] {
        let relative = format!("Contents/Helpers/{name}");
        std::fs::write(app.join(&relative), name).unwrap();
        let mut value = serde_json::json!({"path":relative,"sha256":format!("{:x}",Sha256::digest(name.as_bytes())),"component":name,"source_revision":"a".repeat(40),"target_arch":"aarch64","target_os":"macos"});
        if name == "ori-core" {
            value["features"] = serde_json::json!({"serial":true,"audio":false});
        }
        components.insert(name.to_owned(), value);
    }
    let original = serde_json::json!({"schema_version":1,"release_version":"0.4.0-macos.rc.1","bundle_identifier":"org.openradioinspector.macos.rc","source_revision":"a".repeat(40),"architecture":"arm64","components":components});
    let metadata = app.join("Contents/Resources/rc-build-info.json");
    let bytes = serde_json::to_vec(&original).unwrap();
    std::fs::write(&metadata, &bytes).unwrap();
    assert_eq!(
        bundled_backend_id(&app).unwrap(),
        format!("macos-rc1-{:x}", Sha256::digest(&bytes))
    );
    let mut mixed = original.clone();
    mixed["components"]["ori-core"]["source_revision"] = serde_json::json!("b".repeat(40));
    std::fs::write(&metadata, serde_json::to_vec(&mixed).unwrap()).unwrap();
    assert!(bundled_backend_id(&app).is_err());
    std::fs::write(&metadata, &bytes).unwrap();
    std::fs::write(app.join("Contents/Helpers/ori-core"), "changed").unwrap();
    assert!(bundled_backend_id(&app).is_err());
}
