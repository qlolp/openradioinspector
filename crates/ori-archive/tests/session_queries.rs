//! Session business selectors use the real archive and Python's ordering rules.
use ori_archive::{Archive, ArchiveConfig, ArchiveError, Mutation};
use serde_json::{Value, json};

fn oracle() -> Value {
    serde_json::from_str(include_str!(
        "../../../contracts/fixtures/session-domain-v1.json"
    ))
    .unwrap()
}

async fn fixture(utf16: bool) -> (tempfile::TempDir, Archive) {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("archive.sqlite");
    if utf16 {
        std::fs::copy(
            concat!(
                env!("CARGO_MANIFEST_DIR"),
                "/tests/fixtures/legacy_v5_utf16.sqlite"
            ),
            &path,
        )
        .unwrap();
    }
    let archive = Archive::open(ArchiveConfig::new(path)).unwrap();
    let data = oracle();
    let seed = &data["cases"][0]["seed"];
    for session in seed["sessions"].as_array().unwrap() {
        archive.create_session(session.clone()).await.unwrap();
    }
    archive
        .append_sweeps(seed["sweeps"].as_array().unwrap().clone())
        .await
        .unwrap();
    archive
        .append_observations(seed["observations"].as_array().unwrap().clone())
        .await
        .unwrap();
    (directory, archive)
}

fn candidate(id: &str) -> Value {
    json!({"id":id,"session_id":"session-α","source_stream_id":"spectrum/α",
        "interval":{"started_at":"2026-11-11T12:00:00.123456Z","ended_at":"2026-11-11T12:00:00.123456Z"},
        "center_frequency_hz":100000003,"level_unit":"dBm","state":"queued"})
}

async fn job(archive: &Archive, id: &str, state: &str, priority: u64, at: &str) {
    let source = candidate(&format!("candidate-{id}"));
    archive
        .apply(vec![Mutation::Investigation {
            session_id: "session-α".into(),
            value: json!({"id":id,"candidate_id":source["id"],"target_stream_id":"receiver+α",
            "target_frequency_hz":100000003,"state":state,"priority":priority,"created_at":at}),
            candidate: source,
            event: None,
        }])
        .await
        .unwrap();
}

#[tokio::test]
async fn evidence_matches_exact_frequency_mode_and_start_inclusive_in_utf8_and_utf16() {
    for utf16 in [false, true] {
        let (_directory, archive) = fixture(utf16).await;
        let at = "2026-11-11T14:00:00.123456+02:00";
        let actual = archive
            .matching_observation_ids("session-α", "receiver+α", 100000003, at, Some("FM"), None)
            .await
            .unwrap();
        let data = oracle();
        let expected = data["cases"][0]["actions"]
            .as_array()
            .unwrap()
            .iter()
            .find(|action| action["method"] == "matching_observation_ids")
            .unwrap()["result"]
            .clone();
        assert_eq!(json!(actual), expected);
        assert_eq!(
            archive
                .matching_observation_ids(
                    "session-α",
                    "receiver+α",
                    100000003,
                    at,
                    Some("AM"),
                    None
                )
                .await
                .unwrap(),
            ["wrong-mode"]
        );
        assert_eq!(
            archive
                .matching_observation_ids("session-α", "receiver+α", 100000003, at, None, None)
                .await
                .unwrap()
                .len(),
            4
        );
        assert!(
            archive
                .matching_observation_ids(
                    "session-foreign",
                    "receiver+α",
                    100000003,
                    at,
                    None,
                    None
                )
                .await
                .unwrap()
                .is_empty()
        );
        assert_eq!(
            archive
                .matching_observation_ids(
                    "session-α",
                    "receiver+α",
                    100000003,
                    at,
                    Some("FM"),
                    Some(1)
                )
                .await
                .unwrap(),
            ["tie-\u{e000}"]
        );
        archive.close().await.unwrap();
    }
}

#[tokio::test]
async fn recommendation_and_catalog_use_python_priority_then_utc_and_exact_utf8_id() {
    for utf16 in [false, true] {
        let (_directory, archive) = fixture(utf16).await;
        let at = "2026-11-11T12:00:00.123456Z";
        job(&archive, "low", "queued", 1, at).await;
        job(&archive, "tie-\u{10000}", "queued", 9, at).await;
        job(
            &archive,
            "tie-\u{e000}",
            "queued",
            9,
            "2026-11-11T14:00:00.123456+02:00",
        )
        .await;
        job(&archive, "runtime", "listening", 99, "2026-11-11T12:00:00Z").await;
        job(&archive, "closed", "failed", 999, at).await;
        assert_eq!(
            archive
                .next_queued_investigation("session-α")
                .await
                .unwrap()
                .unwrap()["id"],
            "tie-\u{e000}"
        );
        let list = archive
            .session_investigations("session-α", None)
            .await
            .unwrap();
        assert_eq!(
            list.iter()
                .map(|value| value["id"].as_str().unwrap())
                .collect::<Vec<_>>(),
            ["tie-\u{e000}", "tie-\u{10000}", "low", "runtime", "closed"]
        );
        assert!(
            archive
                .candidate_has_open_investigation("session-α", "candidate-low")
                .await
                .unwrap()
        );
        assert!(
            !archive
                .candidate_has_open_investigation("session-α", "candidate-closed")
                .await
                .unwrap()
        );
        assert!(
            !archive
                .candidate_has_open_investigation("session-foreign", "candidate-low")
                .await
                .unwrap()
        );
        assert!(
            archive
                .receiver_has_runtime_investigation("session-α", "receiver+α", None)
                .await
                .unwrap()
        );
        assert!(
            !archive
                .receiver_has_runtime_investigation("session-α", "receiver+α", Some("runtime"))
                .await
                .unwrap()
        );
        archive.close().await.unwrap();
    }
}

#[tokio::test]
async fn evidence_budget_fails_without_publishing_a_truncated_match_list() {
    let (directory, archive) = fixture(false).await;
    let template = oracle()["cases"][0]["seed"]["observations"][1].clone();
    let observations = (0..32)
        .map(|index| {
            let mut value = template.clone();
            value["id"] = json!(format!("{}-{index}", "x".repeat(90)));
            value["frequency_hz"] = json!(100000003);
            value
        })
        .collect();
    archive.append_observations(observations).await.unwrap();
    archive.close().await.unwrap();
    let mut config = ArchiveConfig::new(directory.path().join("archive.sqlite"));
    config.max_response_bytes = 128;
    let archive = Archive::open(config).unwrap();
    assert!(matches!(
        archive
            .matching_observation_ids(
                "session-α",
                "receiver+α",
                100000003,
                "2026-11-11T12:00:00.123456Z",
                Some("FM"),
                None
            )
            .await,
        Err(ArchiveError::QueryBudget)
    ));
    assert!(matches!(
        archive
            .matching_observation_ids(
                "session-α",
                "receiver+α",
                100000003,
                "2026-11-11T12:00:00.123456Z",
                None,
                Some(0)
            )
            .await,
        Err(ArchiveError::Invalid(_))
    ));
    archive.close().await.unwrap();
}

#[tokio::test]
async fn candidate_and_latest_result_lists_match_python_without_oldest_page_fallback() {
    for utf16 in [false, true] {
        let (_directory, archive) = fixture(utf16).await;
        archive
            .apply(vec![
                Mutation::Candidate {
                    value: candidate("candidate-\u{10000}"),
                    event: None,
                },
                Mutation::Candidate {
                    value: candidate("candidate-\u{e000}"),
                    event: None,
                },
            ])
            .await
            .unwrap();
        let candidates = archive
            .session_candidates("session-α", Some("queued"))
            .await
            .unwrap();
        assert_eq!(
            candidates
                .iter()
                .map(|value| value["id"].as_str().unwrap())
                .collect::<Vec<_>>(),
            ["candidate-\u{e000}", "candidate-\u{10000}"]
        );
        let fixture = oracle();
        let case = fixture["cases"]
            .as_array()
            .unwrap()
            .iter()
            .find(|case| case["name"] == "catalog")
            .unwrap();
        let records = case["actions"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|action| {
                action["method"] == "save_measurement_result" && action.get("result").is_some()
            })
            .map(|action| action["result"].clone())
            .collect::<Vec<_>>();
        for record in &records {
            archive
                .save_measurement_result(record.clone(), None)
                .await
                .unwrap();
        }
        assert_eq!(
            archive
                .session_measurement_results("session-α", None, Some(2))
                .await
                .unwrap(),
            records[records.len() - 2..]
        );
        assert_eq!(
            archive
                .session_measurement_results("session-α", None, None)
                .await
                .unwrap(),
            records
        );
        assert_eq!(
            archive
                .session_measurement_results("session-α", Some("signal_to_noise"), Some(1))
                .await
                .unwrap()
                .len(),
            1
        );
        archive.close().await.unwrap();
    }
}
