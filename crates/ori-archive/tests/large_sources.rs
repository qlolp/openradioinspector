use ori_archive::{Archive, ArchiveConfig, ArchiveError, Mutation};
use serde_json::{Value, json};
use std::{
    io::Write,
    process::{Command, Stdio},
};

fn fixture() -> Value {
    ori_archive::parse_json_value(
        include_bytes!("../../../contracts/fixtures/measurement-v1.json"),
        1_000_000,
    )
    .unwrap()
}
fn session() -> Value {
    let mut value = fixture()["session"].clone();
    value["created_at"] = json!("2026-11-11T12:00:00.123456Z");
    value
}
fn python_accepts(class: &str, value: &Value) -> Value {
    let python = std::env::var_os("ORI_TEST_PYTHON").unwrap_or_else(|| "python".into());
    let mut child = Command::new(python)
        .env("OPENBLAS_NUM_THREADS", "1").env("OMP_NUM_THREADS", "1").env("MKL_NUM_THREADS", "1")
        .args(["-c", "import json,sys; from radioinspector import contracts; source=json.load(sys.stdin); result=getattr(contracts,sys.argv[1]).from_dict(source); print(json.dumps(contracts.to_primitive(result),ensure_ascii=False,allow_nan=False))", class])
        .current_dir(concat!(env!("CARGO_MANIFEST_DIR"), "/../.."))
        .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn().unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(&serde_json::to_vec(value).unwrap())
        .unwrap();
    let output = child.wait_with_output().unwrap();
    assert!(
        output.status.success(),
        "actual Python contract rejected input: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let result = ori_archive::parse_json_value(&output.stdout, 16 * 1024 * 1024).unwrap();
    for key in [
        "id",
        "session_id",
        "stream_id",
        "frequency_hz",
        "center_frequency_hz",
        "frequencies_hz",
    ] {
        if let Some(expected) = value.get(key) {
            assert_eq!(
                result.get(key),
                Some(expected),
                "Python authority changed {key}"
            );
        }
    }
    result
}
fn large_sweep() -> Value {
    let mut value = fixture()["sweep"].clone();
    value["frequencies_hz"] = (0..20_000).map(|i| json!(100_000_001_i64 + i)).collect();
    value["levels"] = (0..20_000)
        .map(|i| json!([-0.0, f64::from_bits(0xbff0000000000001), -100.125][i % 3]))
        .collect();
    value
}

#[tokio::test]
async fn valid_python_large_grid_keeps_all_original_points_through_commit_and_reopen() {
    let source = large_sweep();
    let expected = python_accepts("Sweep", &source);
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("large.sqlite");
    let archive = Archive::open(ArchiveConfig::new(&path)).unwrap();
    archive.create_session(session()).await.unwrap();
    archive.append_sweeps(vec![source.clone()]).await.unwrap();
    archive.close().await.unwrap();
    let archive = Archive::open(ArchiveConfig::new(&path)).unwrap();
    let saved = archive
        .get_sweep(source["id"].as_str().unwrap())
        .await
        .unwrap()
        .unwrap();
    assert_eq!(saved["frequencies_hz"], source["frequencies_hz"]);
    for (a, b) in saved["levels"]
        .as_array()
        .unwrap()
        .iter()
        .zip(source["levels"].as_array().unwrap())
    {
        assert_eq!(a.as_f64().unwrap().to_bits(), b.as_f64().unwrap().to_bits());
    }
    assert_eq!(saved["interval"], expected["interval"]);
    assert_eq!(archive.events(0, 10).await.unwrap().len(), 2);
    archive.close().await.unwrap();
}

#[tokio::test]
async fn archive_frequency_range_keeps_exact_valid_sqlite_integers_beyond_display_precision() {
    let directory = tempfile::tempdir().unwrap();
    let archive = Archive::open(ArchiveConfig::new(directory.path().join("hz.sqlite"))).unwrap();
    archive.create_session(session()).await.unwrap();
    let hz = 9_007_199_254_740_993_i64;
    let mut sweep = fixture()["sweep"].clone();
    sweep["frequencies_hz"] = (0..4).map(|i| json!(hz + i)).collect();
    let mut observation = fixture()["observation"].clone();
    observation["frequency_hz"] = json!(hz);
    observation["level"] = json!(-0.0);
    let candidate = json!({"id":"wide-candidate","session_id":sweep["session_id"],"source_stream_id":sweep["stream_id"],"interval":sweep["interval"],"center_frequency_hz":hz,"bandwidth_hz":1,"peak_level":-0.0,"score":1.0,"level_unit":"dBm","observation_ids":[],"state":"new"});
    python_accepts("Sweep", &sweep);
    python_accepts("ReceiverObservation", &observation);
    python_accepts("Candidate", &candidate);
    archive.append_sweeps(vec![sweep.clone()]).await.unwrap();
    archive
        .append_observations(vec![observation.clone()])
        .await
        .unwrap();
    archive
        .apply(vec![Mutation::Candidate {
            value: candidate.clone(),
            event: None,
        }])
        .await
        .unwrap();
    assert_eq!(
        archive
            .get_sweep(sweep["id"].as_str().unwrap())
            .await
            .unwrap()
            .unwrap()["frequencies_hz"],
        sweep["frequencies_hz"]
    );
    assert_eq!(
        archive
            .get_observation(observation["id"].as_str().unwrap())
            .await
            .unwrap()
            .unwrap()["frequency_hz"],
        hz
    );
    assert_eq!(
        archive
            .get_candidate("wide-candidate")
            .await
            .unwrap()
            .unwrap()["center_frequency_hz"],
        hz
    );
    archive.close().await.unwrap();
}

#[tokio::test]
async fn large_grid_remains_subject_to_explicit_batch_byte_budget_with_atomic_rejection() {
    let directory = tempfile::tempdir().unwrap();
    let mut config = ArchiveConfig::new(directory.path().join("bounded.sqlite"));
    config.max_batch_bytes = 16_384;
    let archive = Archive::open(config).unwrap();
    archive.create_session(session()).await.unwrap();
    let source = large_sweep();
    assert!(matches!(archive.append_sweeps(vec![source.clone()]).await,
        Err(ArchiveError::Invalid(field)) if field == "source batch byte budget"));
    assert!(
        archive
            .get_sweep(source["id"].as_str().unwrap())
            .await
            .unwrap()
            .is_none()
    );
    assert_eq!(archive.events(0, 10).await.unwrap().len(), 1);
    archive.close().await.unwrap();
}
