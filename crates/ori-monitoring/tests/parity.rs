use ori_contracts::{Sweep, from_json_slice, parse_json_value};
use ori_monitoring::*;
use serde_json::Value;

const FIXTURE: &[u8] = include_bytes!("../../../contracts/fixtures/monitoring-detector-v1.json");
fn fixture() -> Value {
    parse_json_value(FIXTURE, FIXTURE.len()).unwrap()
}
fn decode<T: serde::de::DeserializeOwned + ori_contracts::RestoreKeys>(value: &Value) -> T {
    let encoded = serde_json::to_vec(value).unwrap();
    from_json_slice(&encoded, encoded.len()).unwrap()
}
#[test]
fn fingerprint_bytes_match_python_sort_spaces_ascii_escape_and_number_repr() {
    for case in fixture()["fingerprint_cases"].as_array().unwrap() {
        let sweep: Sweep = if let Some(raw) = case["raw_sweep_json"].as_str() {
            from_json_slice(raw.as_bytes(), raw.len()).unwrap()
        } else {
            decode(&case["sweep"])
        };
        assert_text(
            &canonical_fingerprint_json(&sweep).unwrap(),
            case["expected_json"].as_str().unwrap(),
            case["name"].as_str().unwrap(),
        );
        assert_eq!(
            fingerprint(&sweep).unwrap(),
            case["expected"],
            "{}",
            case["name"]
        );
    }
}
#[test]
fn baseline_quantiles_median_mad_source_order_and_cadence_match_python() {
    for case in fixture()["baseline_cases"].as_array().unwrap() {
        let sweeps: Vec<Sweep> = case["sweeps"]
            .as_array()
            .unwrap()
            .iter()
            .map(decode)
            .collect();
        let baseline = build_baseline(&sweeps).unwrap();
        assert_eq!(
            python_canonical_json(&serde_json::to_value(baseline).unwrap()).unwrap(),
            python_canonical_json(&case["expected"]).unwrap(),
            "{}",
            case["name"]
        );
    }
}
#[test]
fn golden_replays_match_decisions_numeric_bits_and_serialized_checkpoints() {
    for case in fixture()["replays"].as_array().unwrap() {
        let initial = if case["initial_state"].is_null() {
            None
        } else {
            Some(case["initial_state"].clone())
        };
        let mut detector = Detector::new(
            decode(&case["baseline"]),
            decode(&case["parameters"]),
            initial,
        )
        .unwrap();
        assert_text(
            &String::from_utf8(detector.checkpoint_json().unwrap()).unwrap(),
            case["expected_initial_checkpoint_json"].as_str().unwrap(),
            case["name"].as_str().unwrap(),
        );
        for (index, step) in case["steps"].as_array().unwrap().iter().enumerate() {
            let sweep: Sweep = decode(&step["sweep"]);
            let allowed: Vec<AllowedSignal> = step["allowed"]
                .as_array()
                .unwrap()
                .iter()
                .map(decode)
                .collect();
            if step.get("error").is_some() {
                let previous = detector.checkpoint_json().unwrap();
                assert!(matches!(
                    detector.step(&sweep, &allowed),
                    Err(MonitoringError::Incompatible)
                ));
                assert_eq!(detector.checkpoint_json().unwrap(), previous);
                continue;
            }
            let actual = detector.step(&sweep, &allowed).unwrap();
            for (field, value) in [
                ("regions", serde_json::to_value(&actual.regions).unwrap()),
                (
                    "excess_db",
                    serde_json::to_value(&actual.excess_db).unwrap(),
                ),
                (
                    "thresholds_db",
                    serde_json::to_value(&actual.thresholds_db).unwrap(),
                ),
            ] {
                assert_eq!(
                    python_canonical_json(&value).unwrap(),
                    python_canonical_json(&step["expected"][field]).unwrap(),
                    "{} step {index} {field}",
                    case["name"]
                );
            }
            assert_eq!(
                String::from_utf8(detector.checkpoint_json().unwrap()).unwrap(),
                step["expected"]["checkpoint_json"],
                "{} step {index}",
                case["name"]
            );
            // A persisted/reloaded checkpoint must produce the same next state.
            detector = Detector::new(
                decode(&case["baseline"]),
                decode(&case["parameters"]),
                Some(detector.checkpoint().unwrap()),
            )
            .unwrap();
        }
    }
}
#[test]
fn stable_removes_only_named_volatile_keys_at_every_nested_object() {
    let value=parse_json_value(br#"{"sequence":1,"nested":{"revision":3,"value":-0.0},"list":[{"connection_id":"gone","value":18446744073709551617}],"revision_name":"keep"}"#,1000).unwrap();
    let actual = stable(&value).unwrap();
    assert_eq!(
        python_canonical_json(&actual).unwrap(),
        r#"{"list": [{"value": 18446744073709551617}], "nested": {"value": -0.0}, "revision_name": "keep"}"#
    );
}
#[test]
fn restore_rejects_nonfinite_bad_dimensions_and_unbounded_history_without_partial_state() {
    let case = &fixture()["baseline_cases"][0];
    let baseline: Baseline = decode(&case["expected"]);
    let parameters: Parameters = decode(&fixture()["replays"][0]["parameters"]);
    for state in [
        serde_json::json!({"window":[],"offsets":[0.0],"active":[]}),
        serde_json::json!({"window":[[true]],"offsets":vec![0.0;9],"active":[]}),
        serde_json::json!({"window":[],"offsets":vec![4.0;9],"active":[]}),
        serde_json::json!({"window":vec![vec![false;9];16],"offsets":vec![0.0;9],"active":[]}),
    ] {
        assert!(Detector::new(baseline.clone(), parameters.clone(), Some(state)).is_err());
    }
    let invalid = b"{\"window\":[],\"offsets\":[NaN],\"active\":[]}";
    assert!(Detector::restore_json(baseline, parameters, invalid).is_err());
}
#[test]
fn incompatible_grid_or_units_and_invalid_baseline_inputs_fail_closed() {
    let case = &fixture()["baseline_cases"][0];
    let mut sweeps: Vec<Sweep> = case["sweeps"]
        .as_array()
        .unwrap()
        .iter()
        .map(decode)
        .collect();
    assert!(build_baseline(&sweeps[..19]).is_err());
    sweeps[1]
        .settings
        .as_mut()
        .unwrap()
        .insert("rbw_hz".into(), serde_json::json!(30000));
    assert!(build_baseline(&sweeps).is_err());
    for kind in 0..5 {
        let mut sweep: Sweep = decode(&case["sweeps"][0]);
        match kind {
            0 => sweep.level_unit = "s_meter_count".into(),
            1 => sweep.settings = None,
            2 => sweep.frequencies_hz[1] = sweep.frequencies_hz[0],
            3 => {
                sweep
                    .settings
                    .as_mut()
                    .unwrap()
                    .insert("overload".into(), Value::Bool(true));
            }
            _ => sweep.levels[0] = f64::INFINITY,
        }
        assert!(fingerprint(&sweep).is_err());
    }
}

fn assert_text(actual: &str, expected: &str, context: &str) {
    if actual != expected {
        let at = actual
            .bytes()
            .zip(expected.bytes())
            .position(|(a, b)| a != b)
            .unwrap_or(actual.len().min(expected.len()));
        let start = at.saturating_sub(40);
        panic!(
            "{context}: difference at byte {at}; actual {:?}; expected {:?}",
            &actual[start..actual.len().min(at + 100)],
            &expected[start..expected.len().min(at + 100)]
        );
    }
}

#[test]
fn window_and_sources_stay_bounded_across_long_lifecycle() {
    let data = fixture();
    let mut parameters: Parameters = decode(&data["replays"][0]["parameters"]);
    parameters.window_sweeps = 15;
    let mut detector = Detector::new(
        decode(&data["baseline_cases"][0]["expected"]),
        parameters,
        None,
    )
    .unwrap();
    for index in 0..500 {
        let mut sweep: Sweep = decode(&data["baseline_cases"][0]["sweeps"][0]);
        sweep.id = format!("bounded-{index}");
        let started = chrono::DateTime::from_timestamp(1_794_398_400 + 4 * index, 0).unwrap();
        sweep.interval.started_at = started.to_rfc3339().parse().unwrap();
        sweep.interval.ended_at = (started + chrono::Duration::seconds(1))
            .to_rfc3339()
            .parse()
            .unwrap();
        assert!(detector.step(&sweep, &[]).unwrap().regions.is_empty());
        assert!(detector.state().window.len() <= 15);
        assert!(detector.state().recent_sources.as_ref().unwrap().len() <= 15);
    }
    assert_eq!(detector.state().window.len(), 15);
    assert!(detector.checkpoint_json().unwrap().len() < 10000);
}
#[test]
fn invalid_baseline_parameters_allowed_or_sweep_cannot_mutate_state() {
    let data = fixture();
    let baseline: Baseline = decode(&data["baseline_cases"][0]["expected"]);
    let parameters: Parameters = decode(&data["replays"][0]["parameters"]);
    for kind in 0..4 {
        let mut bad = baseline.clone();
        match kind {
            0 => bad.p50_dbm[0] = f64::NAN,
            1 => {
                bad.mad_db.pop();
            }
            2 => bad.mad_db[0] = -1.,
            _ => bad.detector_version = "unverified-v2".into(),
        };
        assert!(Detector::new(bad, parameters.clone(), None).is_err());
    }
    for bad in [
        Parameters {
            margin_db: f64::INFINITY,
            ..parameters.clone()
        },
        Parameters {
            confirmations: 6,
            ..parameters.clone()
        },
        Parameters {
            window_sweeps: 16,
            ..parameters.clone()
        },
        Parameters {
            impulse_db: 2.,
            ..parameters.clone()
        },
    ] {
        assert!(Detector::new(baseline.clone(), bad, None).is_err());
    }
    let mut detector = Detector::new(baseline, parameters, None).unwrap();
    let sweep: Sweep = decode(&data["baseline_cases"][0]["sweeps"][0]);
    let before = detector.checkpoint_json().unwrap();
    let bad = AllowedSignal {
        id: "invalid".into(),
        start_hz: 2,
        end_hz: 1,
        watch_changes: false,
        extra: Default::default(),
    };
    assert!(detector.step(&sweep, &[bad]).is_err());
    assert_eq!(detector.checkpoint_json().unwrap(), before);
    let mut bad = sweep;
    bad.levels.pop();
    assert!(detector.step(&bad, &[]).is_err());
    assert_eq!(detector.checkpoint_json().unwrap(), before);
}
#[test]
fn checkpoint_input_bound_and_quality_extraction_preserve_remaining_state() {
    let data = fixture();
    let case = data["replays"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["name"] == "forward_backward_gaps_reset_window")
        .unwrap();
    let checkpoint = case["steps"][2]["expected"]["state"].clone();
    let mut detector = Detector::new(
        decode(&case["baseline"]),
        decode(&case["parameters"]),
        Some(checkpoint),
    )
    .unwrap();
    let gap = detector.take_quality_issue().unwrap();
    assert_eq!(gap.kind, "data_gap");
    assert!(gap.gap_seconds > 0.);
    assert!(detector.take_quality_issue().is_none());
    assert!(
        detector
            .checkpoint()
            .unwrap()
            .get("quality_issue")
            .is_none()
    );
    assert_eq!(detector.state().window.len(), 1);
    let oversized = vec![b' '; MAX_JSON_BYTES + 1];
    assert!(
        Detector::restore_json(
            decode(&case["baseline"]),
            decode(&case["parameters"]),
            &oversized
        )
        .is_err()
    );
}
#[test]
fn short_learning_span_and_strict_boolean_quality_flags_are_preserved() {
    let data = fixture();
    let mut sweeps: Vec<Sweep> = data["baseline_cases"][0]["sweeps"]
        .as_array()
        .unwrap()
        .iter()
        .map(decode)
        .collect();
    for sweep in &mut sweeps {
        sweep.interval = sweeps_interval();
    }
    assert!(build_baseline(&sweeps).is_err());
    let mut sweep: Sweep = decode(&data["baseline_cases"][0]["sweeps"][0]);
    sweep
        .settings
        .as_mut()
        .unwrap()
        .insert("overload".into(), serde_json::json!(1));
    assert!(fingerprint(&sweep).is_ok());
    sweep
        .settings
        .as_mut()
        .unwrap()
        .insert("incomplete".into(), Value::Bool(true));
    assert!(fingerprint(&sweep).is_err());
}
fn sweeps_interval() -> ori_contracts::TimeInterval {
    ori_contracts::TimeInterval {
        started_at: "2026-11-11T12:00:00Z".parse().unwrap(),
        ended_at: "2026-11-11T12:00:01Z".parse().unwrap(),
        extra: Default::default(),
    }
}
