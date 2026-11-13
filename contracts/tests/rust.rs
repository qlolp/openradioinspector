use ori_contracts::{ReceiverObservation, Sweep, from_json_slice, normalize_utc, to_json_vec};
use serde_json::Value;

const FIXTURE: &[u8] = include_bytes!("../fixtures/measurement-v1.json");
const RUNTIME_FIXTURE: &[u8] = include_bytes!("../fixtures/runtime-v1.json");
const HISTORY_FIXTURE: &[u8] = include_bytes!("../fixtures/history-runtime-v1.json");

#[test]
fn core_execution_modes_keep_legacy_capabilities_and_unknown_metadata() {
    use ori_contracts::CoreCapabilities;
    let fixture: Value =
        from_json_slice(include_bytes!("../fixtures/core-execution-v1.json"), 65536).unwrap();
    for payload in fixture["valid"].as_array().unwrap() {
        let decoded: CoreCapabilities =
            from_json_slice(&to_json_vec(payload).unwrap(), 65536).unwrap();
        let encoded: Value = from_json_slice(&to_json_vec(&decoded).unwrap(), 65536).unwrap();
        assert_eq!(&encoded, payload);
    }
    for mode in fixture["invalid_modes"].as_array().unwrap() {
        let payload = serde_json::json!({"features":{},"candidate":{"mode":mode}});
        assert!(
            from_json_slice::<CoreCapabilities>(&to_json_vec(&payload).unwrap(), 65536).is_err()
        );
    }
}

#[test]
fn calibrated_field_shared_fixture_keeps_physical_values_and_required_nullable_uncertainty() {
    use ori_contracts::{FieldStrengthRequest, FieldStrengthResult};
    let fixture: Value = from_json_slice(
        include_bytes!("../../tests/fixtures/field-strength-rms-v2.json"),
        65536,
    )
    .unwrap();
    let request: FieldStrengthRequest =
        from_json_slice(&to_json_vec(&fixture["request"]).unwrap(), 65536).unwrap();
    assert_eq!(request.frequency_hz, 200);
    assert_eq!(
        request.calibration.measurement_chain_id,
        "SYNTHETIC-receiver-antenna-cable-preamp-v1"
    );
    let mut result: FieldStrengthResult =
        from_json_slice(&to_json_vec(&fixture["result"]).unwrap(), 65536).unwrap();
    assert_eq!(result.value_dbuv_per_m, 55.);
    assert_eq!(result.value.to_bits(), 0.000562341325190349_f64.to_bits());
    assert_eq!(result.source.id, "sweep-0");
    assert_eq!(result.uncertainty_db, Some(3.));
    assert_eq!(
        result.calibration.sha256,
        "f049d2a41686fd59a002637a5cde735d3843395308073a74ef4689731eb5b5b6"
    );
    result.uncertainty_db = None;
    let round: FieldStrengthResult =
        from_json_slice(&to_json_vec(&result).unwrap(), 65536).unwrap();
    assert!(round.uncertainty_db.is_none());
    let mut missing = fixture["result"].clone();
    missing.as_object_mut().unwrap().remove("uncertainty_db");
    assert!(
        from_json_slice::<FieldStrengthResult>(&to_json_vec(&missing).unwrap(), 65536).is_err()
    );
    missing["uncertainty_db"] = Value::Null;
    missing["unit"] = serde_json::json!("dBm");
    assert!(
        from_json_slice::<FieldStrengthResult>(&to_json_vec(&missing).unwrap(), 65536).is_err()
    );
}

#[test]
fn history_signed_time_nullable_unsigned_boundary_and_nested_metadata_are_exact() {
    use ori_contracts::runtime::{HistoryJob, HistoryJobResult, HistoryRequest};
    let f: Value = from_json_slice(HISTORY_FIXTURE, HISTORY_FIXTURE.len()).unwrap();
    let request: HistoryRequest =
        from_json_slice(&to_json_vec(&f["request"]).unwrap(), 10000).unwrap();
    let filters = request.filters.as_ref().unwrap();
    assert_eq!(filters.started_at_us, Some(i64::MIN));
    assert_eq!(filters.ended_at_us, Some(i64::MAX));
    assert_eq!(filters.max_frequency_hz, Some(i64::MAX));
    assert_eq!(
        request.threshold.as_ref().unwrap().value.to_bits(),
        (-0.0_f64).to_bits()
    );
    let request: HistoryRequest = from_json_slice(&to_json_vec(&request).unwrap(), 10000).unwrap();
    assert_eq!(
        request.filters.unwrap().session_id.as_deref(),
        Some("history-é-e\u{301}/雪")
    );
    let job: HistoryJob = from_json_slice(&to_json_vec(&f["job"]).unwrap(), 10000).unwrap();
    assert_eq!(job.source_boundary, Some(u64::MAX));
    let job: HistoryJob = from_json_slice(&to_json_vec(&job).unwrap(), 10000).unwrap();
    assert_eq!(job.source_boundary, Some(u64::MAX));
    assert!(job.failure.is_none());
    let future = &job.enforcement.unwrap().extra["future"];
    assert_eq!(future["é"], 1);
    assert_eq!(future["e\u{301}"], 2);
    assert_eq!(future["$serde_json::private::Number"], "literal");
    assert_eq!(
        future["$ori::escaped::$serde_json::private::Number"],
        "literal-prefix"
    );
    assert_eq!(future["big"].to_string(), "18446744073709551616");
    assert_eq!(
        future["negative_zero"].as_f64().unwrap().to_bits(),
        (-0.0_f64).to_bits()
    );
    assert_eq!(
        future["adjacent"].as_f64().unwrap().to_bits(),
        0xbff0000000000001
    );
    let failed: HistoryJob =
        from_json_slice(&to_json_vec(&f["failed_job"]).unwrap(), 10000).unwrap();
    assert!(failed.source_boundary.is_none() && failed.enforcement.is_none());
    assert_eq!(failed.failure.unwrap().code, "source_record_invalid");
    let output: HistoryJobResult =
        from_json_slice(&to_json_vec(&f["result"]).unwrap(), 10000).unwrap();
    let output: HistoryJobResult = from_json_slice(&to_json_vec(&output).unwrap(), 10000).unwrap();
    assert_eq!(output.metrics["io_bytes"].as_u64(), Some(u64::MAX));
    assert_eq!(
        output.metrics["negative_zero"].as_f64().unwrap().to_bits(),
        (-0.0_f64).to_bits()
    );
    assert_eq!(
        output.metrics["$serde_json::private::Number"],
        "metrics-key"
    );
    assert_eq!(
        output.enforcement.extra["future"]["$serde_json::private::Number"],
        "result-enforcement-key"
    );
    assert_eq!(output.data["é"], 1);
    assert_eq!(output.data["e\u{301}"], 2);
}

#[test]
fn history_viewport_retains_original_source_identity_unit_point_and_float_bits() {
    use ori_contracts::runtime::{HistoryViewportPage, HistoryViewportQuery};
    let f: Value = from_json_slice(HISTORY_FIXTURE, HISTORY_FIXTURE.len()).unwrap();
    let query: HistoryViewportQuery =
        from_json_slice(&to_json_vec(&f["viewport_query"]).unwrap(), 10000).unwrap();
    assert_eq!(
        (query.start_us, query.end_us),
        (Some(i64::MIN), Some(i64::MAX))
    );
    assert!(query.cursor.is_none());
    let page: HistoryViewportPage =
        from_json_slice(&to_json_vec(&f["viewport_page"]).unwrap(), 10000).unwrap();
    let page: HistoryViewportPage = from_json_slice(&to_json_vec(&page).unwrap(), 10000).unwrap();
    assert!(page.derived);
    assert_eq!(page.boundary, u64::MAX);
    let row = &page.rows[0];
    assert_eq!((row.started_at_us, row.ended_at_us), (i64::MIN, i64::MAX));
    assert_eq!(row.measurement_id, "sweep-e\u{301}/雪");
    assert_eq!(row.level_unit, "s_meter_count");
    assert!(row.settings_sha256.is_none());
    assert_eq!(row.cells[0].level.to_bits(), (-0.0_f64).to_bits());
    assert_eq!(row.cells[1].level.to_bits(), 0xbff0000000000001);
    assert_eq!(
        (row.cells[1].frequency_hz, row.cells[1].point_index),
        (i64::MAX, 65535)
    );
    let cursor = page.next_cursor.unwrap();
    assert_eq!(cursor.boundary, u64::MAX);
    assert_eq!(
        cursor.after.started_at.as_str(),
        "1969-12-31T23:59:59.999999Z"
    );
}

#[test]
fn history_integer_formats_reject_overflow_missing_required_null_and_negative_frequency() {
    use ori_contracts::runtime::{HistoryJob, HistoryRequest};
    for token in ["-1", "18446744073709551616"] {
        let f = std::str::from_utf8(HISTORY_FIXTURE)
            .unwrap()
            .replace("18446744073709551615", token);
        let f: Value = from_json_slice(f.as_bytes(), f.len()).unwrap();
        assert!(from_json_slice::<HistoryJob>(&to_json_vec(&f["job"]).unwrap(), 10000).is_err());
    }
    let mut f: Value = from_json_slice(HISTORY_FIXTURE, HISTORY_FIXTURE.len()).unwrap();
    f["job"].as_object_mut().unwrap().remove("source_boundary");
    assert!(from_json_slice::<HistoryJob>(&to_json_vec(&f["job"]).unwrap(), 10000).is_err());
    f["request"]["filters"]["min_frequency_hz"] = serde_json::json!(-1);
    assert!(
        from_json_slice::<HistoryRequest>(&to_json_vec(&f["request"]).unwrap(), 10000).is_err()
    );
    f["request"]["filters"]["min_frequency_hz"] = serde_json::json!(0);
    for token in ["-9223372036854775809", "9223372036854775808"] {
        let request = String::from_utf8(to_json_vec(&f["request"]).unwrap())
            .unwrap()
            .replace("-9223372036854775808", token);
        assert!(from_json_slice::<HistoryRequest>(request.as_bytes(), 10000).is_err());
    }
    for page_size in [0, 501] {
        f["request"]["page_size"] = serde_json::json!(page_size);
        assert!(
            from_json_slice::<HistoryRequest>(&to_json_vec(&f["request"]).unwrap(), 10000).is_err()
        );
    }
    for columns in [0, 1025] {
        f["viewport_query"]["columns"] = serde_json::json!(columns);
        assert!(
            from_json_slice::<ori_contracts::runtime::HistoryViewportQuery>(
                &to_json_vec(&f["viewport_query"]).unwrap(),
                10000
            )
            .is_err()
        );
    }
}

#[test]
fn history_operations_require_their_independent_route_feature() {
    use ori_contracts::runtime::{JSONObject, select_operations};
    let empty = JSONObject::new();
    let ids =
        |features: &[&str]| select_operations("rust-candidate", features, &[], &empty).unwrap();
    assert!(
        !ids(&[])
            .iter()
            .any(|op| op.operation_id == "post_api_v1_history_jobs")
    );
    assert!(
        ids(&["derived_history_jobs"])
            .iter()
            .any(|op| op.operation_id == "post_api_v1_history_jobs")
    );
    assert!(
        !ids(&["derived_history_jobs"])
            .iter()
            .any(|op| op.operation_id == "get_api_v1_history_waterfall")
    );
    assert!(
        ids(&["derived_history_viewport"])
            .iter()
            .any(|op| op.operation_id == "get_api_v1_history_waterfall")
    );
    assert!(
        !select_operations("python-legacy", &[], &[], &empty)
            .unwrap()
            .iter()
            .any(|op| op.operation_id == "post_api_v1_history_jobs")
    );
}

#[test]
fn runtime_dtos_preserve_exact_values_and_existing_summary_schema() {
    use ori_contracts::runtime::{
        BrowserPairing, MeasurementWebSocket, NativePairing, SecurityStatus,
    };
    let fixture: Value = from_json_slice(RUNTIME_FIXTURE, RUNTIME_FIXTURE.len()).unwrap();
    let status: SecurityStatus =
        from_json_slice(&to_json_vec(&fixture["security_status"]).unwrap(), 10000).unwrap();
    assert!(!status.pairing_required);
    let round: Value = from_json_slice(&to_json_vec(&status).unwrap(), 10000).unwrap();
    assert_eq!(round["mode"], "legacy_loopback");
    assert_eq!(round["future_metadata"]["big"].as_u64(), Some(u64::MAX));
    assert_eq!(
        round["future_metadata"]["negative_zero"]
            .as_f64()
            .unwrap()
            .to_bits(),
        (-0.0_f64).to_bits()
    );
    assert_eq!(
        round["future_metadata"]["adjacent"]
            .as_f64()
            .unwrap()
            .to_bits(),
        0xbff0000000000001
    );
    assert_eq!(round["future_metadata"]["é"], 1);
    assert_eq!(round["future_metadata"]["e\u{301}"], 2);
    assert_eq!(
        round["future_metadata"]["$serde_json::private::Number"],
        "literal"
    );
    let _: BrowserPairing =
        from_json_slice(&to_json_vec(&fixture["browser_pairing"]).unwrap(), 10000).unwrap();
    let native: NativePairing =
        from_json_slice(&to_json_vec(&fixture["native_pairing"]).unwrap(), 10000).unwrap();
    assert_eq!(native.token, "synthetic-fixture-never-a-valid-credential");
    let mut socket: MeasurementWebSocket = from_json_slice(
        &to_json_vec(&fixture["websocket_descriptor"]).unwrap(),
        10000,
    )
    .unwrap();
    assert_eq!(socket.path, "/api/v1/events/ws");
    socket.max_frame_bytes = u64::MAX;
    let socket: MeasurementWebSocket =
        from_json_slice(&to_json_vec(&socket).unwrap(), 10000).unwrap();
    assert_eq!(socket.max_frame_bytes, u64::MAX);
    let page: ori_contracts::SweepSummaryPage =
        from_json_slice(&to_json_vec(&fixture["summary_page"]).unwrap(), 10000).unwrap();
    assert_eq!(page.next_cursor, None);
    assert_eq!(page.items[0].peak_level.to_bits(), (-0.0_f64).to_bits());
}

#[test]
fn runtime_backend_and_request_features_fail_closed() {
    use ori_contracts::runtime::{JSONObject, select_operations};
    let canonical: Value = from_json_slice(
        ori_contracts::RUNTIME_OPENAPI_V1_JSON.as_bytes(),
        ori_contracts::RUNTIME_OPENAPI_V1_JSON.len(),
    )
    .unwrap();
    assert_eq!(canonical["x-ori-contract-variant"], "rust-candidate");
    assert_eq!(
        canonical["paths"]["/api/v1/security/status"]["get"]["operationId"],
        "get_api_v1_security_status"
    );
    assert_eq!(
        ori_contracts::runtime::OPERATIONS
            .iter()
            .find(|op| op.operation_id == "post_api_v1_security_pair")
            .unwrap()
            .max_request_bytes,
        Some(4096)
    );
    assert_eq!(
        ori_contracts::runtime::OPERATIONS
            .iter()
            .find(|op| op.operation_id == "get_api_v1_sessions_id_sweep_summaries")
            .unwrap()
            .path_parameters,
        ["id"]
    );
    let empty = JSONObject::new();
    assert!(select_operations("unknown", &[], &[], &empty).is_err());
    assert_eq!(
        select_operations("python-legacy", &[], &[], &empty)
            .unwrap()
            .len(),
        136
    );
    let available = select_operations("rust-candidate", &[], &[], &empty).unwrap();
    assert!(
        available
            .iter()
            .any(|op| op.operation_id == "get_api_v1_security_status")
    );
    assert!(
        !available
            .iter()
            .any(|op| op.operation_id == "get_api_v1_events_ws")
    );
    assert!(
        select_operations("rust-candidate", &["sweep_summaries"], &[], &empty)
            .unwrap()
            .iter()
            .any(|op| op.operation_id == "get_api_v1_sessions_id_sweep_summaries")
    );
    assert!(
        select_operations(
            "rust-candidate",
            &[],
            &["ori.measurements.v1.protobuf"],
            &empty
        )
        .unwrap()
        .iter()
        .any(|op| op.operation_id == "get_api_v1_events_ws")
    );
    let request = JSONObject::from([("enabled".into(), Value::Bool(true))]);
    assert!(
        !select_operations("rust-candidate", &["control_task_planning"], &[], &request)
            .unwrap()
            .iter()
            .any(|op| op.operation_id == "post_api_v1_task_scheduler")
    );
}

#[test]
fn runtime_websocket_descriptor_refuses_unknown_protocol_version() {
    let mut fixture: Value = from_json_slice(RUNTIME_FIXTURE, RUNTIME_FIXTURE.len()).unwrap();
    fixture["websocket_descriptor"]["version"] = Value::from(2);
    assert!(
        from_json_slice::<ori_contracts::runtime::MeasurementWebSocket>(
            &to_json_vec(&fixture["websocket_descriptor"]).unwrap(),
            10000
        )
        .is_err()
    );
}

#[test]
fn python_integer_zero_semantics_are_distinct_from_explicit_float_zero() {
    let value = ori_contracts::parse_json_value(br#"{"integer":-0,"float":-0.0}"#, 1000).unwrap();
    assert_eq!(value["integer"].to_string(), "0");
    assert_eq!(
        value["float"].as_f64().unwrap().to_bits(),
        (-0.0_f64).to_bits()
    );
}

#[test]
fn required_nullable_cursor_round_trips_and_cannot_disappear() {
    let payload = br#"{"items":[],"next_cursor":null}"#;
    let value: ori_contracts::SweepSummaryPage = from_json_slice(payload, 1000).unwrap();
    let round: Value = from_json_slice(&to_json_vec(&value).unwrap(), 1000).unwrap();
    assert!(round.get("next_cursor").unwrap().is_null());
    assert!(from_json_slice::<ori_contracts::SweepSummaryPage>(br#"{"items":[]}"#, 1000).is_err());
}

#[test]
fn golden_preserves_measurements_and_normalizes_only_timestamps() {
    let fixture: Value = from_json_slice(FIXTURE, FIXTURE.len()).unwrap();
    let sweep: Sweep =
        from_json_slice(&serde_json::to_vec(&fixture["sweep"]).unwrap(), 10000).unwrap();
    assert_eq!(sweep.id, "fixture-sweep-α");
    assert_eq!(sweep.session_id, "fixture-сессия");
    assert_eq!(sweep.stream_id, "spectrum/one");
    assert_eq!(
        sweep.frequencies_hz,
        [100000001, 100000002, 100000003, 100000004]
    );
    assert_eq!(
        sweep.interval.started_at.as_str(),
        "2026-11-11T12:00:00.123456Z"
    );
    assert_eq!(
        sweep.interval.ended_at.as_str(),
        "2026-11-11T12:00:00.223456Z"
    );
    assert_eq!(
        sweep.levels.iter().map(|f| f.to_bits()).collect::<Vec<_>>(),
        [
            0xc059080000000000,
            0x8000000000000000,
            0xbff0000000000001,
            0xc040600000000000
        ]
    );
    let encoded = to_json_vec(&sweep).unwrap();
    let round: Sweep = from_json_slice(&encoded, encoded.len()).unwrap();
    assert_eq!(round.levels[1].to_bits(), 0x8000000000000000);
    assert_eq!(round.levels[2].to_bits(), 0xbff0000000000001);
    assert_eq!(round.settings.as_ref().unwrap()["rbw_hz"], 10000);
    let observation: ReceiverObservation =
        from_json_slice(&serde_json::to_vec(&fixture["observation"]).unwrap(), 10000).unwrap();
    assert_eq!(observation.stream_id, "receiver+one");
    assert_eq!(observation.level.unwrap().to_bits(), 137.0_f64.to_bits());
    assert_eq!(observation.level_unit, "s_meter_count");
    assert_eq!(
        observation.metadata.as_ref().unwrap()["hardware_validated"],
        false
    );
}

#[test]
fn unknown_nested_metadata_and_exact_integer_tokens_survive() {
    let payload = r#"{"id":"x/%+?&=雪","session_id":"s","stream_id":"r","interval":{"started_at":"2026-11-11 12:00:00.000001","ended_at":"2026-11-11T12:00:01Z","new_interval_field":{"a":-0.0}},"frequency_hz":9007199254740993,"mode":"FM","level_unit":"s_meter_count","metadata":{"big":18446744073709551615,"nested":{"level":-0.0}},"new_field":[1,{"x":2}]}"#;
    let value: ReceiverObservation = from_json_slice(payload.as_bytes(), payload.len()).unwrap();
    assert_eq!(value.frequency_hz, 9007199254740993);
    assert_eq!(value.id, "x/%+?&=雪");
    assert_eq!(
        value.metadata.as_ref().unwrap()["big"].as_u64(),
        Some(u64::MAX)
    );
    let encoded = to_json_vec(&value).unwrap();
    let round: Value = from_json_slice(&encoded, encoded.len()).unwrap();
    assert_eq!(round["new_field"][1]["x"], 2);
    assert_eq!(
        round["interval"]["new_interval_field"]["a"]
            .as_f64()
            .unwrap()
            .to_bits(),
        (-0.0_f64).to_bits()
    );
    assert_eq!(
        round["metadata"]["nested"]["level"]
            .as_f64()
            .unwrap()
            .to_bits(),
        (-0.0_f64).to_bits()
    );
}

#[test]
fn strict_json_rejects_ambiguity_overflow_and_nonfinite_before_decode() {
    for invalid in [
        r#"{"x":1,"x":2}"#,
        r#"{"x":{"a":1,"\u0061":2}}"#,
        r#"[{"a":0,"a":1}]"#,
        r#"{"x":NaN}"#,
        r#"{"x":Infinity}"#,
        r#"{"x":1e400}"#,
        r#"{"x":1} trailing"#,
        r#"[1,]"#,
        "",
    ] {
        assert!(
            from_json_slice::<Value>(invalid.as_bytes(), 1000).is_err(),
            "accepted {invalid}"
        );
    }
    assert!(from_json_slice::<Value>(&[0xff], 1).is_err());
    let valid = "{\"雪\":0}".as_bytes();
    assert!(from_json_slice::<Value>(valid, valid.len()).is_ok());
    assert!(from_json_slice::<Value>(valid, valid.len() - 1).is_err());
    for value in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
        assert!(to_json_vec(&vec![Some(value)]).is_err());
    }
}

#[test]
fn legacy_offsets_keep_microseconds_without_float_epoch_math() {
    for (input, expected) in [
        ("2026-11-11 12:00:00", "2026-11-11T12:00:00.000000Z"),
        (
            "2026-11-11T15:00:00.123456789+03:00",
            "2026-11-11T12:00:00.123456Z",
        ),
        (
            "2026-01-01T00:00:00.000001+01:00",
            "2025-12-31T23:00:00.000001Z",
        ),
        ("1969-12-31T23:59:59.999999Z", "1969-12-31T23:59:59.999999Z"),
    ] {
        assert_eq!(normalize_utc(input).unwrap(), expected);
    }
    assert!(normalize_utc("2026-02-30T12:00:00Z").is_err());
}

#[test]
fn complete_f64_range_and_unicode_codepoints_remain_exact() {
    let values = [f64::from_bits(1), f64::MAX, -0.0, -1.0000000000000002];
    let encoded = to_json_vec(&values).unwrap();
    let round: Vec<f64> = from_json_slice(&encoded, encoded.len()).unwrap();
    assert_eq!(
        round.iter().map(|v| v.to_bits()).collect::<Vec<_>>(),
        [
            1,
            0x7fefffffffffffff,
            0x8000000000000000,
            0xbff0000000000001
        ]
    );
    assert_eq!(
        ori_contracts::encode_path_segment("x/%+?&=雪"),
        "x%2F%25%2B%3F%26%3D%E9%9B%AA"
    );
    let text = r#"{"é":1,"e\u0301":2,"id":"é-e\u0301"}"#;
    let value: Value = from_json_slice(text.as_bytes(), text.len()).unwrap();
    assert_eq!(value.as_object().unwrap().len(), 3);
    assert_eq!(
        value["id"].as_str().unwrap().as_bytes(),
        &[0xc3, 0xa9, 0x2d, 0x65, 0xcc, 0x81]
    );
    assert!(from_json_slice::<Value>(br#""\ud800""#, 1000).is_err());
}

#[test]
fn envelope_versions_and_invalid_legacy_timestamps_are_rejected() {
    for text in [
        r#"{"contract":"wrong","contract_version":1,"data":{}}"#,
        r#"{"contract":"openradioinspector-platform","contract_version":2,"data":{}}"#,
    ] {
        assert!(from_json_slice::<ori_contracts::Envelope>(text.as_bytes(), 1000).is_err());
    }
    let text = r#"{"id":"s","name":"s","streams":[],"state":"draft","created_at":"2026-11-11T12:00:00Z","contract_version":2}"#;
    assert!(from_json_slice::<ori_contracts::Session>(text.as_bytes(), 1000).is_err());
    assert!(normalize_utc("2026-11-11T12:00:60Z").is_err());
}

#[test]
fn arbitrary_precision_metadata_remains_raw_but_nonfinite_decimals_are_rejected() {
    let text =
        r#"{"metadata":{"n":18446744073709551616,"negative":-18446744073709551616,"level":-0.0}}"#;
    let value: Value = from_json_slice(text.as_bytes(), text.len()).unwrap();
    assert_eq!(value["metadata"]["n"].to_string(), "18446744073709551616");
    assert_eq!(
        value["metadata"]["negative"].to_string(),
        "-18446744073709551616"
    );
    let encoded = to_json_vec(&value).unwrap();
    let round: Value = from_json_slice(&encoded, encoded.len()).unwrap();
    assert_eq!(round["metadata"]["n"].to_string(), "18446744073709551616");
    assert_eq!(
        round["metadata"]["level"].as_f64().unwrap().to_bits(),
        0x8000000000000000
    );
    let invalid = Value::Number("1e999".parse().unwrap());
    assert!(to_json_vec(&invalid).is_err());
}

#[test]
fn real_serde_protocol_names_and_escape_prefixes_are_preserved_as_metadata() {
    const CASES: &[u8] = include_bytes!("metadata-edge-cases.json");
    let raw = ori_contracts::parse_json_value(CASES, CASES.len()).unwrap();
    assert_eq!(raw["sole"]["$serde_json::private::Number"], "1e999");
    let value: Value = from_json_slice(CASES, CASES.len()).unwrap();
    assert_eq!(value["sole"]["$serde_json::private::Number"], "1e999");
    assert_eq!(value["multiple"]["$serde_json::private::Number"], "123");
    assert_eq!(value["multiple"]["ordinary"], true);
    assert_eq!(
        value["prefix_collision"]["$serde_json::private::Number"],
        "raw-key"
    );
    assert_eq!(
        value["prefix_collision"]["$ori::escaped::$serde_json::private::Number"],
        "literal-prefix"
    );
    assert_eq!(
        value["prefix_collision"]["$ori::escaped::$ori::escaped::$serde_json::private::Number"],
        "double-prefix"
    );
    assert_eq!(value["huge_integer"].to_string(), "18446744073709551616");
    assert_eq!(value["unicode"]["é"]["e\u{301}"], "ID/雪+%?");
    assert_eq!(value["unicode"]["e\u{301}"], "distinct");
    let encoded = to_json_vec(&value).unwrap();
    let round = ori_contracts::parse_json_value(&encoded, encoded.len()).unwrap();
    assert_eq!(round, raw);
    let mut fixture = ori_contracts::parse_json_value(FIXTURE, FIXTURE.len()).unwrap();
    fixture["sweep"]["settings"] = raw.clone();
    fixture["sweep"]["future"] = raw.clone();
    fixture["sweep"]["interval"]["future"] = raw.clone();
    let encoded = to_json_vec(&fixture["sweep"]).unwrap();
    let typed: Sweep = from_json_slice(&encoded, encoded.len()).unwrap();
    assert_eq!(
        typed.settings.as_ref().unwrap()["sole"]["$serde_json::private::Number"],
        "1e999"
    );
    assert_eq!(typed.extra["future"], raw);
    assert_eq!(typed.interval.extra["future"], raw);
}
