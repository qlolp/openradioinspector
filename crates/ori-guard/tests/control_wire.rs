use ori_guard::WorkerStatus;

#[test]
fn worker_identity_is_internal_and_open_legacy_metadata_remains_exact() {
    let raw=br#"{"session_id":"s/%+","stream_id":"r?","state":"running","alive":true,"current":{"count":1208925819614629174706176,"negative":-0.0,"object":{"$serde_json::private::Number":"source"}},"future":{"nested":{"$serde_json::private::RawValue":"original"}}}"#;
    let mut worker: WorkerStatus = ori_contracts::from_json_slice(raw, 4096).unwrap();
    worker.worker_instance = Some("opaque-internal-generation".into());
    let wire = ori_contracts::parse_json_value(&ori_contracts::to_json_vec(&worker).unwrap(), 4096)
        .unwrap();
    let original = ori_contracts::parse_json_value(raw, 4096).unwrap();
    assert_eq!(wire, original);
    assert_eq!(
        wire["current"]["negative"].as_f64().unwrap().to_bits(),
        0x8000000000000000
    );
    assert_eq!(
        wire["current"]["count"].to_string(),
        "1208925819614629174706176"
    );
    assert!(wire.get("worker_instance").is_none());
}
