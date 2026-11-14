fn main() {
    #[cfg(windows)]
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
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
