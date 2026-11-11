use ori_contracts::{ReceiverObservation, Sweep, from_json_slice, normalize_utc, to_json_vec};
use serde_json::Value;

const FIXTURE: &[u8] = include_bytes!("../fixtures/measurement-v1.json");

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
