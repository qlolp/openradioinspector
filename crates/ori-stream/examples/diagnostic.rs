//! Explicit local host diagnostic, separate from test/build acceptance gates.
use ori_contracts::{parse_json_value, to_json_vec};
use ori_stream::{MAX_FRAME_BYTES, decode_frame, encode_frame};
use serde_json::Value;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let fixtures = parse_json_value(
        include_bytes!("../../../contracts/fixtures/stream-v1.json"),
        2_000_000,
    )?;
    let mut envelope = parse_json_value(
        fixtures["valid"][0]["input_json"]
            .as_str()
            .unwrap()
            .as_bytes(),
        MAX_FRAME_BYTES,
    )?;
    envelope["data"]["payload"]["sweep"]["frequencies_hz"] = (0..4096)
        .map(|i| Value::from(100_000_001_u64 + i))
        .collect();
    envelope["data"]["payload"]["sweep"]["levels"] = (0..4096)
        .map(|i| Value::from(-100.125 + f64::from(i % 257) * 0.000_031))
        .collect();
    let json = to_json_vec(&envelope)?;
    let protobuf = encode_frame(&json)?;
    if let Some(directory) = std::env::args_os().nth(1) {
        let directory = std::path::PathBuf::from(directory);
        std::fs::create_dir_all(&directory)?;
        std::fs::write(directory.join("synthetic.json"), &json)?;
        std::fs::write(directory.join("synthetic.protobuf"), &protobuf)?;
    }
    let start = std::time::Instant::now();
    for _ in 0..20 {
        std::hint::black_box(decode_frame(&protobuf)?);
    }
    let binary_time = start.elapsed();
    let start = std::time::Instant::now();
    for _ in 0..20 {
        std::hint::black_box(parse_json_value(&json, MAX_FRAME_BYTES)?);
    }
    println!(
        "synthetic4096 JSON={} protobuf={} decode20_binary={binary_time:?} decode20_json={:?}; host diagnostic only",
        json.len(),
        protobuf.len(),
        start.elapsed()
    );
    Ok(())
}
