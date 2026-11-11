use ori_contracts::{parse_json_value, to_json_vec};
use ori_stream::{
    Limits, MAX_FRAME_BYTES, MAX_POINTS, capability, decode_frame, decode_frame_with_limits,
    encode_frame, encode_frame_with_limits, validate_frame, validate_frame_with_limits,
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

fn fixtures() -> Value {
    parse_json_value(
        include_bytes!("../../../contracts/fixtures/stream-v1.json"),
        2_000_000,
    )
    .unwrap()
}

fn unhex(text: &str) -> Vec<u8> {
    let (pairs, remainder) = text.as_bytes().as_chunks::<2>();
    assert!(remainder.is_empty());
    pairs
        .iter()
        .map(|pair| u8::from_str_radix(std::str::from_utf8(pair).unwrap(), 16).unwrap())
        .collect()
}

#[test]
fn independent_idl_oracle_and_all_valid_frames() {
    let fixtures = fixtures();
    let hash = Sha256::digest(include_bytes!(
        "../../../contracts/protobuf/measurement_stream.proto"
    ));
    assert_eq!(
        hash.iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>(),
        fixtures["proto_sha256"]
    );
    for case in fixtures["valid"].as_array().unwrap() {
        let name = case["name"].as_str().unwrap();
        let frame = unhex(case["wire_hex"].as_str().unwrap());
        let decoded = decode_frame(&frame).unwrap_or_else(|error| panic!("{name}: {error}"));
        assert_eq!(decoded.protocol_version, 1, "{name}");
        assert_eq!(decoded.sequence.to_string(), case["sequence"], "{name}");
        assert_eq!(
            decoded.envelope_json,
            case["metadata_json"].as_str().unwrap().as_bytes(),
            "{name}"
        );
        let metadata = parse_json_value(
            case["metadata_json"].as_str().unwrap().as_bytes(),
            MAX_FRAME_BYTES,
        )
        .unwrap();
        assert_eq!(decoded.envelope, metadata, "{name}");
        assert_eq!(
            decoded.spectrum.is_some(),
            case["typed"].as_bool().unwrap(),
            "{name}"
        );
        if let Some(spectrum) = &decoded.spectrum {
            assert_eq!(
                spectrum
                    .frequencies_hz
                    .iter()
                    .map(u64::to_string)
                    .collect::<Vec<_>>(),
                case["frequencies_hz"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|v| v.as_str().unwrap().to_owned())
                    .collect::<Vec<_>>(),
                "{name}"
            );
            assert_eq!(
                spectrum
                    .levels
                    .iter()
                    .map(|v| format!("{:016x}", v.to_bits()))
                    .collect::<Vec<_>>(),
                case["level_bits"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|v| v.as_str().unwrap().to_owned())
                    .collect::<Vec<_>>(),
                "{name}"
            );
        }
        let restored = decoded.into_envelope();
        let original = parse_json_value(
            case["input_json"].as_str().unwrap().as_bytes(),
            MAX_FRAME_BYTES,
        )
        .unwrap();
        let mut original_metadata = original.clone();
        let mut restored_metadata = restored.clone();
        if case["typed"] == true {
            for value in [&mut original_metadata, &mut restored_metadata] {
                let sweep = value["data"]["payload"]["sweep"].as_object_mut().unwrap();
                sweep.remove("levels");
                sweep.remove("frequencies_hz");
            }
            let original_levels = original["data"]["payload"]["sweep"]["levels"]
                .as_array()
                .unwrap();
            let restored_levels = restored["data"]["payload"]["sweep"]["levels"]
                .as_array()
                .unwrap();
            assert_eq!(
                original_levels
                    .iter()
                    .map(|v| v.as_f64().unwrap().to_bits())
                    .collect::<Vec<_>>(),
                restored_levels
                    .iter()
                    .map(|v| v.as_f64().unwrap().to_bits())
                    .collect::<Vec<_>>(),
                "{name}"
            );
            assert_eq!(
                original["data"]["payload"]["sweep"]["frequencies_hz"],
                restored["data"]["payload"]["sweep"]["frequencies_hz"],
                "{name}"
            );
        }
        assert_eq!(restored_metadata, original_metadata, "{name}");
        let encoded = encode_frame(case["input_json"].as_str().unwrap().as_bytes()).unwrap();
        assert_eq!(
            encoded,
            unhex(case["canonical_hex"].as_str().unwrap()),
            "{name}"
        );
    }
}

#[test]
fn all_malformed_or_ambiguous_frames_are_rejected() {
    for case in fixtures()["invalid"].as_array().unwrap() {
        assert!(
            decode_frame(&unhex(case["wire_hex"].as_str().unwrap())).is_err(),
            "{}",
            case["name"]
        );
    }
}

#[test]
fn every_incomplete_prefix_is_rejected_without_unbounded_allocation() {
    let fixtures = fixtures();
    let wire = unhex(fixtures["valid"][0]["wire_hex"].as_str().unwrap());
    for length in 0..wire.len() {
        assert!(decode_frame(&wire[..length]).is_err(), "prefix {length}");
    }
}

#[test]
fn lowering_only_limits_and_generic_point_budgets() {
    let fixtures = fixtures();
    let first = &fixtures["valid"][0];
    let bytes = first["input_json"].as_str().unwrap().as_bytes();
    let wire = unhex(first["wire_hex"].as_str().unwrap());
    assert_eq!(
        encode_frame_with_limits(
            bytes,
            Limits {
                max_frame_bytes: wire.len(),
                max_points: MAX_POINTS
            }
        )
        .unwrap(),
        wire
    );
    let too_small = Limits {
        max_frame_bytes: wire.len() - 1,
        max_points: MAX_POINTS,
    };
    assert!(decode_frame_with_limits(&wire, too_small).is_err());
    assert!(encode_frame_with_limits(bytes, too_small).is_err());
    let points = Limits {
        max_frame_bytes: MAX_FRAME_BYTES,
        max_points: 3,
    };
    assert!(decode_frame_with_limits(&wire, points).is_err());
    assert!(encode_frame_with_limits(bytes, points).is_err());
    for limits in [
        Limits {
            max_frame_bytes: MAX_FRAME_BYTES + 1,
            max_points: MAX_POINTS,
        },
        Limits {
            max_frame_bytes: MAX_FRAME_BYTES,
            max_points: MAX_POINTS + 1,
        },
    ] {
        assert!(decode_frame_with_limits(&wire, limits).is_err());
        assert!(encode_frame_with_limits(bytes, limits).is_err());
    }
    let generic = fixtures["valid"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["name"] == "integer-level-kind-generic")
        .unwrap();
    assert!(
        decode_frame_with_limits(
            &unhex(generic["wire_hex"].as_str().unwrap()),
            Limits {
                max_frame_bytes: MAX_FRAME_BYTES,
                max_points: 2
            }
        )
        .is_err()
    );
    let unknown = fixtures["valid"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["name"] == "unknown-event-generic")
        .unwrap();
    assert!(
        decode_frame_with_limits(
            &unhex(unknown["wire_hex"].as_str().unwrap()),
            Limits {
                max_frame_bytes: MAX_FRAME_BYTES,
                max_points: 0
            }
        )
        .is_ok()
    );
}

#[test]
fn strict_types_and_fallback_preserve_original_generic_json() {
    let fixtures = fixtures();
    let first = fixtures["valid"][0]["input_json"].as_str().unwrap();
    for bad in [
        first.replace("\"sequence\":1", "\"sequence\":1.0"),
        first.replace("\"contract_version\":1", "\"contract_version\":1.0"),
        first.replace(
            "\"occurred_at\":\"2026-11-11T12:00:00.223456Z\"",
            "\"occurred_at\":true",
        ),
    ] {
        assert!(encode_frame(bad.as_bytes()).is_err());
    }
    for case in fixtures["valid"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|case| case["typed"] == false)
    {
        let decoded =
            decode_frame(&encode_frame(case["input_json"].as_str().unwrap().as_bytes()).unwrap())
                .unwrap();
        assert_eq!(
            decoded.envelope_json,
            case["input_json"].as_str().unwrap().as_bytes()
        );
        assert!(decoded.spectrum.is_none());
    }
}

#[test]
fn capability_is_only_library_metadata() {
    assert_eq!(
        capability(),
        json!({"version":1,"transport":"websocket","encoding":"protobuf","path":"/api/v1/events/ws","subprotocol":"ori.measurements.v1.protobuf","max_frame_bytes":MAX_FRAME_BYTES})
    );
}

#[test]
fn forwarding_validator_checks_everything_without_reconstructing_or_decoding_vectors() {
    let fixtures = fixtures();
    for case in fixtures["valid"].as_array().unwrap() {
        let bytes = unhex(case["wire_hex"].as_str().unwrap());
        let header = validate_frame(&bytes).unwrap();
        assert_eq!(header.protocol_version, 1);
        assert_eq!(header.sequence.to_string(), case["sequence"]);
        assert_eq!(header.has_spectrum, case["typed"].as_bool().unwrap());
    }
    for case in fixtures["invalid"].as_array().unwrap() {
        assert!(
            validate_frame(&unhex(case["wire_hex"].as_str().unwrap())).is_err(),
            "{}",
            case["name"]
        );
    }
    let first = unhex(fixtures["valid"][0]["wire_hex"].as_str().unwrap());
    assert!(
        validate_frame_with_limits(
            &first,
            Limits {
                max_frame_bytes: MAX_FRAME_BYTES,
                max_points: 3
            }
        )
        .is_err()
    );
}

#[test]
fn lossless_4096_point_binary_is_smaller_than_standard_json() {
    let fixtures = fixtures();
    let mut original = parse_json_value(
        fixtures["valid"][0]["input_json"]
            .as_str()
            .unwrap()
            .as_bytes(),
        MAX_FRAME_BYTES,
    )
    .unwrap();
    original["data"]["payload"]["sweep"]["frequencies_hz"] = (0..4096)
        .map(|i| Value::from(100_000_001_u64 + i))
        .collect();
    original["data"]["payload"]["sweep"]["levels"] = (0..4096)
        .map(|i| Value::from(-100.125 + f64::from(i % 257) * 0.000_031))
        .collect();
    let json = to_json_vec(&original).unwrap();
    let binary = encode_frame(&json).unwrap();
    assert!(binary.len() < json.len());
    let decoded = decode_frame(&binary).unwrap();
    assert_eq!(decoded.spectrum.as_ref().unwrap().levels.len(), 4096);
    assert_eq!(
        decoded.spectrum.as_ref().unwrap().frequencies_hz[4095],
        100_004_096
    );
}

#[test]
fn typed_metadata_is_canonical_under_workspace_ordered_json_features() {
    fn reverse_objects(value: &mut Value) {
        match value {
            Value::Object(map) => {
                let fields = std::mem::take(map).into_iter().rev();
                for (key, mut value) in fields {
                    reverse_objects(&mut value);
                    map.insert(key, value);
                }
            }
            Value::Array(values) => values.iter_mut().for_each(reverse_objects),
            _ => {}
        }
    }
    let fixtures = fixtures();
    let case = &fixtures["valid"][0];
    let mut source = parse_json_value(
        case["input_json"].as_str().unwrap().as_bytes(),
        MAX_FRAME_BYTES,
    )
    .unwrap();
    reverse_objects(&mut source);
    let encoded = encode_frame(&to_json_vec(&source).unwrap()).unwrap();
    assert_eq!(encoded, unhex(case["canonical_hex"].as_str().unwrap()));
    assert_eq!(
        decode_frame(&encoded).unwrap().envelope_json,
        case["metadata_json"].as_str().unwrap().as_bytes()
    );
}
