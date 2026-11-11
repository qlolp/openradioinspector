use ori_archive::{Archive, ArchiveConfig, parse_json_value};
use serde::{Deserialize, Serialize};

const METADATA: &str = r#"{"rbw_hz":10000,"integer":-0,"float":-0.0,"large":1208925819614629174706176,"$serde_json::private::Number":"literal metadata"}"#;

#[derive(Serialize, Deserialize)]
struct NumericMetadata {
    rbw_hz: i64,
    integer: i64,
    float: f64,
    large: serde_json::Number,
    #[serde(rename = "$serde_json::private::Number")]
    literal: String,
}

#[test]
fn lexical_integer_negative_zero_is_python_integer_zero_but_float_negative_zero_keeps_its_bits() {
    let value = parse_json_value(METADATA.as_bytes(), 4096).unwrap();
    assert_eq!(serde_json::to_string(&value["integer"]).unwrap(), "0");
    assert_eq!(
        value["integer"].as_f64().unwrap().to_bits(),
        0.0_f64.to_bits()
    );
    assert_eq!(
        value["float"].as_f64().unwrap().to_bits(),
        (-0.0_f64).to_bits()
    );
    let typed: NumericMetadata = serde_json::from_value(value).unwrap();
    assert_eq!(typed.integer, 0);
    assert_eq!(typed.float.to_bits(), (-0.0_f64).to_bits());
    assert_eq!(typed.large.to_string(), "1208925819614629174706176");
    assert_eq!(typed.literal, "literal metadata");
    let encoded = serde_json::to_vec(&typed).unwrap();
    let roundtrip = parse_json_value(&encoded, 4096).unwrap();
    assert_eq!(roundtrip["integer"].to_string(), "0");
    assert_eq!(
        roundtrip["float"].as_f64().unwrap().to_bits(),
        (-0.0_f64).to_bits()
    );
}

#[tokio::test]
async fn historical_metadata_text_is_byte_unchanged_while_query_values_use_python_integer_semantics()
 {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("legacy.sqlite");
    std::fs::copy(
        concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/tests/fixtures/legacy_v5_utf16.sqlite"
        ),
        &path,
    )
    .unwrap();
    {
        let db = rusqlite::Connection::open(&path).unwrap();
        db.execute(
            "UPDATE sweeps SET settings_json=? WHERE id='arrival-last'",
            [METADATA],
        )
        .unwrap();
    }
    let archive = Archive::open(ArchiveConfig::new(&path)).unwrap();
    let source = archive.get_sweep("arrival-last").await.unwrap().unwrap();
    assert_eq!(source["settings"]["integer"].to_string(), "0");
    assert_eq!(
        source["settings"]["float"].as_f64().unwrap().to_bits(),
        (-0.0_f64).to_bits()
    );
    assert_eq!(
        source["settings"]["large"].to_string(),
        "1208925819614629174706176"
    );
    assert_eq!(
        source["settings"]["$serde_json::private::Number"],
        "literal metadata"
    );
    let (session, sources, _) = archive
        .analysis_inputs("fixture-сессия", vec!["arrival-last".into()], vec![])
        .await
        .unwrap();
    assert_eq!(session["id"], "fixture-сессия");
    assert_eq!(sources[0], source);
    archive.close().await.unwrap();
    let db =
        rusqlite::Connection::open_with_flags(path, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)
            .unwrap();
    let raw: String = db
        .query_row(
            "SELECT settings_json FROM sweeps WHERE id='arrival-last'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(raw.as_bytes(), METADATA.as_bytes());
}
