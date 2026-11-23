fn main() {
    // Make the installed UI report the same source as its bundled helpers.
    println!("cargo:rerun-if-changed=runtime/rc-build-info.json");
    if let Ok(bytes) = std::fs::read("runtime/rc-build-info.json") {
        let manifest: serde_json::Value =
            serde_json::from_slice(&bytes).expect("valid staged RC manifest");
        let source = manifest["source_revision"]
            .as_str()
            .expect("staged RC revision");
        assert!(source.len() == 40 && source.bytes().all(|byte| byte.is_ascii_hexdigit()));
        println!("cargo:rustc-env=ORI_BUILD_SOURCE_SHA={source}");
        let version = manifest["release_version"]
            .as_str()
            .expect("staged version");
        assert!(matches!(version, "0.4.0-rc.1" | "0.4.0"));
        println!("cargo:rustc-env=ORI_INSTALLER_VERSION={version}");
    }
    #[cfg(windows)]
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "local_node",
            "choose_local_archive_directory",
            "connect_node",
            "pair_node",
            "revoke_pairing",
            "api_request",
            "cancel_request",
            "start_events",
            "stop_events",
            "ack_event",
            "open_legacy",
        ]),
    ))
    .expect("Windows application configuration must be valid");
}
