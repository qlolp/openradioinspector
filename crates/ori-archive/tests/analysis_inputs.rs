use ori_archive::{Archive, ArchiveConfig, ArchiveError, Mutation};
use serde_json::{Value, json};

fn session(id: &str, unit: &str) -> Value {
    json!({"id":id,"name":"Synthetic analysis snapshot","created_at":"2026-11-11T00:00:00.123457Z",
        "metadata":{"generation":0},"streams":[{"id":format!("{id}/analyzer"),"device":"synthetic",
        "role":"spectrum_analyzer","level_unit":unit}]})
}
fn stamp(offset: i64) -> String {
    let base = chrono::DateTime::parse_from_rfc3339("2026-11-11T00:00:00.123457Z").unwrap();
    (base + chrono::TimeDelta::microseconds(offset))
        .to_rfc3339_opts(chrono::SecondsFormat::Micros, true)
}
fn sweep(id: &str, sid: &str, unit: &str, offset: i64, generation: i64) -> Value {
    json!({"id":id,"session_id":sid,"stream_id":format!("{sid}/analyzer"),
        "interval":{"started_at":stamp(offset),"ended_at":stamp(offset+10)},
        "frequencies_hz":[100,200],"levels":[-0.0,-90.00000000000001],"level_unit":unit,
        "settings":{"rbw_hz":10000,"generation":generation,"source":{"exact":1.0000000000000002,
            "negative_zero":-0.0,"reserved":{"$serde_json::private::Number":"raw science metadata"}}}})
}
fn candidate(id: &str, sid: &str, generation: i64) -> Value {
    json!({"id":id,"session_id":sid,"source_stream_id":format!("{sid}/analyzer"),
        "interval":{"started_at":stamp(0),"ended_at":stamp(10)},"center_frequency_hz":100,
        "level_unit":"dBm","state":"new","score":generation})
}

#[tokio::test]
async fn domain_read_modify_commit_coordinator_is_shared_by_all_archive_clones() {
    let dir = tempfile::tempdir().unwrap();
    let archive = Archive::open(ArchiveConfig::new(dir.path().join("archive.sqlite"))).unwrap();
    let cloned = archive.clone();
    assert!(std::sync::Arc::ptr_eq(
        &archive.domain_coordinator(),
        &cloned.domain_coordinator()
    ));
    archive.close().await.unwrap();
}

#[tokio::test]
async fn analysis_sources_are_found_only_scoped_ordered_duplicates_and_exact_raw_values() {
    let dir = tempfile::tempdir().unwrap();
    let archive = Archive::open(ArchiveConfig::new(dir.path().join("archive.sqlite"))).unwrap();
    archive.create_session(session("own", "dBm")).await.unwrap();
    archive
        .create_session(session("foreign", "dBm"))
        .await
        .unwrap();
    archive
        .append_sweeps(vec![
            sweep(" own-𐀀 ", "own", "dBm", 0, 0),
            sweep("foreign-source", "foreign", "dBm", 0, 0),
        ])
        .await
        .unwrap();
    archive
        .apply(vec![
            Mutation::Candidate {
                value: candidate("own-candidate", "own", 0),
                event: None,
            },
            Mutation::Candidate {
                value: candidate("foreign-candidate", "foreign", 0),
                event: None,
            },
        ])
        .await
        .unwrap();
    let (selected, sources, candidates) = archive
        .analysis_inputs(
            "own",
            vec![
                " own-𐀀 ".into(),
                "absent".into(),
                "foreign-source".into(),
                " own-𐀀 ".into(),
            ],
            vec![
                "own-candidate".into(),
                "absent".into(),
                "foreign-candidate".into(),
                "own-candidate".into(),
            ],
        )
        .await
        .unwrap();
    assert_eq!(selected, archive.get_session("own").await.unwrap().unwrap());
    assert_eq!(sources.len(), 2);
    assert_eq!(
        sources[0],
        archive.get_sweep(" own-𐀀 ").await.unwrap().unwrap()
    );
    assert_eq!(sources[0], sources[1]);
    assert_eq!(
        sources[0]["levels"][0].as_f64().unwrap().to_bits(),
        (-0.0_f64).to_bits()
    );
    assert_eq!(
        sources[0]["levels"][1].as_f64().unwrap().to_bits(),
        (-90.00000000000001_f64).to_bits()
    );
    assert_eq!(
        sources[0]["interval"]["started_at"],
        "2026-11-11T00:00:00.123457Z"
    );
    assert_eq!(
        sources[0]["settings"]["source"]["reserved"]["$serde_json::private::Number"],
        "raw science metadata"
    );
    assert_eq!(candidates.len(), 2);
    assert_eq!(candidates[0], candidates[1]);
    assert_eq!(candidates[0]["id"], "own-candidate");
    assert!(matches!(
        archive.analysis_inputs("absent", vec![], vec![]).await,
        Err(ArchiveError::NotFound)
    ));
    assert!(matches!(
        archive
            .analysis_inputs("own", vec!["x".into(); 34], vec![])
            .await,
        Err(ArchiveError::Invalid(_))
    ));
    assert!(matches!(
        archive
            .analysis_inputs("own", vec![], vec!["x".into(); 1001])
            .await,
        Err(ArchiveError::Invalid(_))
    ));
    archive.close().await.unwrap();
}

#[tokio::test]
async fn analysis_context_keeps_actual_nearest_incompatible_raw_sources_for_python_validation() {
    let dir = tempfile::tempdir().unwrap();
    let archive = Archive::open(ArchiveConfig::new(dir.path().join("archive.sqlite"))).unwrap();
    archive
        .create_session(session("raw", "dBµV"))
        .await
        .unwrap();
    archive
        .append_sweeps(vec![
            sweep("anchor", "raw", "dBµV", 0, 0),
            sweep("nearest-incompatible", "raw", "dBµV", 1000, 1),
            sweep("farther-compatible", "raw", "dBµV", 2000, 0),
        ])
        .await
        .unwrap();
    let (selected, context) = archive
        .analysis_context("raw", "anchor", 0, 1)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(selected["id"], "raw");
    assert_eq!(context.anchor["level_unit"], "dBµV");
    assert_eq!(context.after.len(), 1);
    assert_eq!(context.after[0]["id"], "nearest-incompatible");
    assert_eq!(context.after[0]["settings"]["generation"], 1);
    assert!(archive.sweep_context("raw", "anchor", 0, 1).await.is_err());
    assert!(
        archive
            .analysis_context("raw", "absent", 0, 1)
            .await
            .unwrap()
            .is_none()
    );
    archive
        .create_session(session("foreign", "dBm"))
        .await
        .unwrap();
    assert!(
        archive
            .analysis_context("foreign", "anchor", 0, 1)
            .await
            .unwrap()
            .is_none()
    );
    assert!(matches!(
        archive.analysis_context("absent", "anchor", 0, 1).await,
        Err(ArchiveError::NotFound)
    ));
    assert!(matches!(
        archive.analysis_context("raw", "anchor", 17, 0).await,
        Err(ArchiveError::Invalid(_))
    ));
    archive.close().await.unwrap();
}

#[tokio::test]
async fn combined_analysis_response_budget_counts_session_and_all_sources() {
    let dir = tempfile::tempdir().unwrap();
    let mut config = ArchiveConfig::new(dir.path().join("archive.sqlite"));
    config.max_response_bytes = 2048;
    let archive = Archive::open(config).unwrap();
    archive.create_session(session("own", "dBm")).await.unwrap();
    archive
        .append_sweeps(vec![sweep("one", "own", "dBm", 0, 0)])
        .await
        .unwrap();
    assert!(archive.get_session("own").await.unwrap().is_some());
    assert!(archive.get_sweep("one").await.unwrap().is_some());
    assert!(matches!(
        archive
            .analysis_inputs("own", vec!["one".into(); 33], vec![])
            .await,
        Err(ArchiveError::QueryBudget)
    ));
    archive.close().await.unwrap();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn source_and_context_inputs_observe_one_atomic_snapshot_during_concurrent_writes() {
    let dir = tempfile::tempdir().unwrap();
    let archive = Archive::open(ArchiveConfig::new(dir.path().join("archive.sqlite"))).unwrap();
    archive.create_session(session("own", "dBm")).await.unwrap();
    archive
        .append_sweeps(vec![
            sweep("anchor", "own", "dBm", 0, 0),
            sweep("next-0", "own", "dBm", 1000000, 0),
        ])
        .await
        .unwrap();
    archive
        .apply(vec![Mutation::Candidate {
            value: candidate("mutable", "own", 0),
            event: None,
        }])
        .await
        .unwrap();
    let writer = archive.clone();
    let updates = tokio::spawn(async move {
        for generation in 1..=64 {
            let mut value = session("own", "dBm");
            value["metadata"]["generation"] = json!(generation);
            writer
                .apply(vec![
                    Mutation::SaveSession { value, event: None },
                    Mutation::Candidate {
                        value: candidate("mutable", "own", generation),
                        event: None,
                    },
                    Mutation::Sweeps(vec![sweep(
                        &format!("next-{generation}"),
                        "own",
                        "dBm",
                        1000000 - generation,
                        generation,
                    )]),
                ])
                .await
                .unwrap();
        }
    });
    for _ in 0..64 {
        let (session, _, candidates) = archive
            .analysis_inputs("own", vec![], vec!["mutable".into()])
            .await
            .unwrap();
        assert_eq!(
            session["metadata"]["generation"].as_i64().unwrap() as f64,
            candidates[0]["score"].as_f64().unwrap()
        );
        let (session, context) = archive
            .analysis_context("own", "anchor", 0, 1)
            .await
            .unwrap()
            .unwrap();
        assert_eq!(
            session["metadata"]["generation"],
            context.after[0]["settings"]["generation"]
        );
    }
    updates.await.unwrap();
    let (session, context) = archive
        .analysis_context("own", "anchor", 0, 1)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(session["metadata"]["generation"], 64);
    assert_eq!(context.after[0]["settings"]["generation"], 64);
    archive.close().await.unwrap();
}
