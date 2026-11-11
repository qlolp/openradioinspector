use ori_control::{Operation, OperationKind, parse_operation, parse_submission};
use serde_json::json;

#[test]
fn admission_matches_python_shape_but_detailed_errors_remain_async() {
    let request = parse_submission(br#"{"operation":"setting","parameters":{"name":"output_enabled","value":"yes"},"request_id":"owned-1"}"#, None).unwrap();
    assert_eq!(request.kind(), OperationKind::Setting);
    assert!(request.operation().is_err());
    for raw in [
        br#"{"operation":"raw","parameters":{"command":"output on"}}"#.as_slice(),
        br#"{"operation":"capture","surprise":true}"#,
        br#"{"operation":"inspect","parameters":[]}"#,
    ] {
        assert!(parse_submission(raw, None).is_err());
    }
    assert!(parse_submission(br#"{"operation":"lease.acquire"}"#, None).is_err());
    assert!(
        parse_submission(
            br#"{"operation":"lease.acquire"}"#,
            Some("owned-client-0123456789")
        )
        .is_ok()
    );
}
#[test]
fn typed_allowlist_and_defaults_are_real_python_forms() {
    assert_eq!(OperationKind::ALL.len(), 29);
    let values = [
        ("inspect", json!({})),
        ("capture", json!({})),
        ("lease.acquire", json!({"acknowledge_rf":true})),
        ("lease.renew", json!({})),
        ("lease.release", json!({})),
        ("monitor.resume", json!({})),
        ("setting", json!({"name":"rbw_hz","value":30000})),
        ("touch", json!({"x":479,"y":319})),
        ("release", json!({})),
        ("keypad", json!({"text":"-12.25M"})),
        ("menu.home", json!({})),
        ("sd.list", json!({})),
        ("sd.read", json!({})),
        ("sd.delete", json!({"confirm_delete":true})),
        ("preset.save", json!({"confirm":true})),
        ("preset.load", json!({"confirm":true})),
        ("acquire.raw", json!({})),
        ("acquire.zero", json!({})),
        ("service.guide", json!({})),
        ("trace", json!({})),
        ("marker", json!({})),
        ("measurement", json!({})),
        ("preset.export", json!({})),
        (
            "preset.import",
            json!({"confirm":true,"document":"{\"schema_version\":1,\"profile\":\"tinysa-ultra-zs405-control-v1\",\"settings\":{\"rbw_hz\":30000}}"}),
        ),
        ("selftest.start", json!({"confirm":true})),
        ("generator.sweep", json!({"confirm":true})),
        ("output.off", json!({})),
        ("audio.inputs", json!({})),
        ("audio.capture", json!({"confirm_connection":true})),
    ];
    for (name, parameters) in values {
        let kind = OperationKind::parse(name).unwrap();
        assert_eq!(
            parse_operation(kind, &parameters).unwrap().kind(),
            kind,
            "{name}"
        );
    }
    match parse_operation(OperationKind::AcquireRaw, &json!({})).unwrap() {
        Operation::AcquireRaw(range) => {
            assert_eq!(range.start_hz, None);
            assert_eq!(range.points, None)
        }
        _ => panic!(),
    }
}
#[test]
fn malformed_preset_and_rf_in_receive_preset_are_rejected_without_a_console_escape() {
    for document in [
        r#"{"schema_version":1,"profile":"tinysa-ultra-zs405-control-v1","settings":{"repeat":2,"repeat":3}}"#,
        r#"{"schema_version":1,"profile":"tinysa-ultra-zs405-control-v1","settings":{"output_enabled":true}}"#,
        r#"{"schema_version":1,"profile":"tinysa-ultra-zs405-control-v1","settings":{"center_hz":100000000}}"#,
        r#"{"schema_version":1,"profile":"tinysa-ultra-zs405-control-v1","settings":{"repeat":NaN}}"#,
    ] {
        assert!(
            parse_operation(
                OperationKind::PresetImport,
                &json!({"confirm":true,"document":document})
            )
            .is_err()
        );
    }
    let document = r#"{"schema_version":1,"profile":"tinysa-ultra-zs405-control-v1","settings":{"rbw_hz":30000,"repeat":3,"attenuation_db":0}}"#;
    match parse_operation(
        OperationKind::PresetImport,
        &json!({"confirm":true,"document":document}),
    )
    .unwrap()
    {
        Operation::PresetImport { settings, .. } => {
            assert!(matches!(settings[0], ori_instruments::Setting::RbwHz(_)));
            assert!(matches!(settings[1], ori_instruments::Setting::Repeat(3)));
        }
        _ => panic!(),
    }
}
#[test]
fn integer_fields_do_not_accept_boolean_float_or_injected_text() {
    for parameters in [
        json!({"name":"start_hz","value":true}),
        json!({"name":"points","value":3.0}),
        json!({"name":"modulation","value":"fm\routput on"}),
        json!({"name":"lna","value":1}),
    ] {
        assert!(parse_operation(OperationKind::Setting, &parameters).is_err());
    }
    assert!(parse_operation(OperationKind::Touch, &json!({"x":480,"y":0})).is_err());
    assert!(parse_operation(OperationKind::Keypad, &json!({"text":"1\routput on"})).is_err());
    assert!(parse_operation(OperationKind::SdRead, &json!({"filename":"../private"})).is_err());
    assert!(
        parse_operation(
            OperationKind::AudioCapture,
            &json!({"confirm_connection":true,"sample_rate_hz":44100})
        )
        .is_err()
    );
}

#[test]
fn reserved_json_object_keys_cannot_turn_an_object_into_a_scalar_preset_command() {
    for value in [
        json!({"$serde_json::private::Number":"2"}),
        json!({"$serde_json::private::RawValue":"2"}),
    ] {
        let document=ori_contracts::to_json_vec(&json!({"schema_version":1,"profile":"tinysa-ultra-zs405-control-v1","settings":{"repeat":value}})).unwrap();
        assert!(
            parse_operation(
                OperationKind::PresetImport,
                &json!({"confirm":true,"document":String::from_utf8(document).unwrap()})
            )
            .is_err()
        );
    }
}
