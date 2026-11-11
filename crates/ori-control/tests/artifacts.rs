use ori_control::*;
use serde_json::json;
use sha2::{Digest, Sha256};
#[test]
fn every_rgb565_word_matches_python_pixels_and_is_exactly_reversible() {
    let oracle: serde_json::Value = serde_json::from_str(include_str!(
        "../../ori-instruments/tests/fixtures/control_python.json"
    ))
    .unwrap();
    let raw: Vec<u8> = (0..480 * 320)
        .flat_map(|n| ((n % 65536) as u16).to_be_bytes())
        .collect();
    let png = screen_png(&raw).unwrap();
    let decoder = png::Decoder::new(std::io::Cursor::new(&png));
    let mut reader = decoder.read_info().unwrap();
    let mut rgb = vec![0; reader.output_buffer_size().unwrap()];
    let info = reader.next_frame(&mut rgb).unwrap();
    rgb.truncate(info.buffer_size());
    assert_eq!(
        Sha256::digest(&rgb)
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect::<String>(),
        oracle["decoded_rgb_sha256"]
    );
    let reconstructed: Vec<u8> = rgb
        .as_chunks::<3>()
        .0
        .iter()
        .flat_map(|p| {
            let r = (p[0] as u32 * 31 + 127) / 255;
            let g = (p[1] as u32 * 63 + 127) / 255;
            let b = (p[2] as u32 * 31 + 127) / 255;
            ((r << 11 | g << 5 | b) as u16).to_be_bytes()
        })
        .collect();
    assert_eq!(reconstructed, raw);
    assert!(screen_png(&raw[..raw.len() - 1]).is_err());
}
#[test]
fn artifact_hash_is_actual_payload_and_filename_metadata_and_size_are_bounded() {
    let target = Target {
        session_id: "s".into(),
        stream_id: "t".into(),
    };
    let raw = b"exact\0SD\xffch> ".to_vec();
    let item=ClosedArtifact::from_payload(target.clone(),ArtifactKind::SdFile,"application/octet-stream","TRACE.CSV",raw.clone(),json!({"occurred_at":"2026-11-11T12:00:00.123456Z","opaque":{"$serde_json::private::Number":"123"}}),1024).unwrap();
    assert_eq!(item.payload(), raw);
    assert_eq!(
        item.sha256(),
        Sha256::digest(&raw)
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect::<String>()
    );
    assert_eq!(item.summary()["size_bytes"], raw.len());
    for (name, meta, limit) in [
        (
            "../private",
            json!({"occurred_at":"2026-11-11T00:00:00Z"}),
            1024,
        ),
        ("safe", json!({"occurred_at":"invalid"}), 1024),
        ("safe", json!({"occurred_at":"2026-11-11T00:00:00Z"}), 1),
    ] {
        assert!(
            ClosedArtifact::from_payload(
                target.clone(),
                ArtifactKind::SdFile,
                "application/octet-stream",
                name,
                raw.clone(),
                meta,
                limit
            )
            .is_err()
        );
    }
}
#[test]
fn raw_document_retains_bits_wire_grid_and_interval_without_a_canonical_sweep() {
    let at = chrono::DateTime::parse_from_rfc3339("2026-11-11T12:00:00.123456Z")
        .unwrap()
        .to_utc();
    let sample = ori_instruments::RawSpectrumSample {
        kind: ori_instruments::SpectrumKind::RawSpectrum,
        frequencies_hz: vec![100000000, 100333333],
        levels: vec![-0.0, f64::from_bits(1)],
        level_unit: "dBm".into(),
        interval: ori_instruments::OperationInterval {
            started_at: at,
            ended_at: at,
        },
        settings: json!({"requested_stop_hz":101000000,"stop_is_inclusive":false}),
        wire: vec![b'{', b'x', 0, 1, b'x', 0, 2, b'}'],
        zero_db: 174,
    };
    let doc = raw_document(&sample).unwrap();
    let bytes = ori_contracts::to_json_vec(&doc).unwrap();
    let parsed = ori_contracts::parse_json_value(&bytes, 65536).unwrap();
    assert_eq!(
        parsed["levels"][0].as_f64().unwrap().to_bits(),
        (-0.0f64).to_bits()
    );
    assert_eq!(parsed["levels"][1].as_f64().unwrap().to_bits(), 1);
    assert_eq!(parsed["frequencies_hz"][1], 100333333);
    assert_eq!(parsed["sample_offsets_seconds"], serde_json::Value::Null);
    assert_eq!(parsed["timing_source"], "interval_only");
}
