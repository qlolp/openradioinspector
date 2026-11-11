fn main() {
    #[cfg(windows)]
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "connect_node",
            "api_request",
            "cancel_request",
            "start_events",
            "stop_events",
            "open_legacy",
        ]),
    ))
    .expect("Windows application configuration must be valid");
}
