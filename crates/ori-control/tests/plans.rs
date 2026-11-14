use ori_control::*;
use ori_instruments::*;
use serde_json::json;
fn state() -> TinySaState {
    TinySaState {
        device_mode: DeviceMode::Input,
        control_mode: ControlMode::Monitoring,
        restart_required: false,
        closed: false,
        profile_verified: true,
        control_profile_verified: true,
        settings_source: "command_acknowledged".into(),
        settings: json!({"start_hz":100000000,"stop_hz":101000000,"points":3,"ultra":false}),
        device_mode_readback: false,
        generator_output_readback: false,
        settings_readback: false,
        hardware_validation: "not_run".into(),
    }
}
#[test]
fn omitted_ranges_use_the_owned_state_at_execution_start_and_science_reuses_source() {
    let raw = parse_operation(OperationKind::AcquireRaw, &json!({})).unwrap();
    let mut current = state();
    current.settings["points"] = json!(7);
    let plan = plan_operation(&raw, &current).unwrap();
    assert!(matches!(
        &plan.commands[0],
        AdapterCommand::TinySa(TinySaRequest::AcquireRaw(RawScanConfig { points: 7, .. }))
    ));
    let measurement = parse_operation(OperationKind::Measurement, &json!({"points":3})).unwrap();
    let plan = plan_operation(&measurement, &current).unwrap();
    assert_eq!(plan.commands.len(), 2);
    assert!(matches!(
        plan.commands[1],
        AdapterCommand::TinySa(TinySaRequest::ReadRbw)
    ));
    current.device_mode = DeviceMode::Unknown;
    assert!(plan_operation(&raw, &current).is_err());
}
#[test]
fn typed_plans_reuse_existing_registry_and_do_not_resume_or_enable_rf_implicitly() {
    let current = state();
    for kind in OperationKind::ALL {
        let params = match kind {
            OperationKind::LeaseAcquire => json!({"acknowledge_rf":true}),
            OperationKind::Setting => json!({"name":"lna","value":false}),
            OperationKind::Touch => json!({"x":0,"y":0}),
            OperationKind::Keypad => json!({"text":"100M"}),
            OperationKind::SdDelete => json!({"confirm_delete":true}),
            OperationKind::PresetSave
            | OperationKind::PresetLoad
            | OperationKind::SelfTest
            | OperationKind::GeneratorSweep => json!({"confirm":true}),
            OperationKind::AudioCapture => json!({"confirm_connection":true}),
            OperationKind::PresetImport => {
                json!({"confirm":true,"document":"{\"schema_version\":1,\"profile\":\"tinysa-ultra-zs405-control-v1\",\"settings\":{}}"})
            }
            _ => json!({}),
        };
        let operation = parse_operation(*kind, &params).unwrap();
        let plan = plan_operation(&operation, &current).unwrap();
        for command in plan.commands {
            if let AdapterCommand::TinySa(TinySaRequest::Control(control)) = command {
                assert!(
                    !matches!(control, TinySaControl::ResumeMonitoring)
                        || *kind == OperationKind::MonitorResume
                );
                assert!(
                    !matches!(control, TinySaControl::GeneratorSweep(_))
                        || *kind == OperationKind::GeneratorSweep
                );
            }
        }
    }
}
#[test]
fn marker_read_and_peak_do_not_advance_but_changes_and_output_off_do() {
    for (action, expected) in [
        ("read", RevisionEffect::Unchanged),
        ("peak", RevisionEffect::Unchanged),
        ("on", RevisionEffect::AdvanceAfterAcknowledgement),
    ] {
        let op = parse_operation(OperationKind::Marker, &json!({"action":action})).unwrap();
        assert_eq!(
            plan_operation(&op, &state()).unwrap().revision_effect,
            expected
        );
    }
    assert_eq!(
        plan_operation(&Operation::OutputOff, &state())
            .unwrap()
            .revision_effect,
        RevisionEffect::AdvanceAfterAcknowledgement
    );
}
