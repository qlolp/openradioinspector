use ori_archive::{
    Archive, ArchiveConfig, ArchiveError, ScopedSnapshotExport, ScopedSnapshotKind,
    ScopedSnapshotQuery, SnapshotExport, SnapshotExportLimits, SnapshotExportQuery,
};
use serde_json::{Value, json};
use std::time::Duration;

fn fixture() -> Value {
    ori_archive::parse_json_value(
        include_bytes!("../../../contracts/fixtures/measurement-v1.json"),
        1024 * 1024,
    )
    .unwrap()
}

async fn seeded() -> (tempfile::TempDir, Archive) {
    let root = tempfile::tempdir().unwrap();
    let archive = Archive::open(ArchiveConfig::new(root.path().join("source.sqlite"))).unwrap();
    let mut session = fixture()["session"].clone();
    session["created_at"] = json!("2026-11-11T12:00:00.123456Z");
    archive.create_session(session).await.unwrap();
    archive
        .append_sweeps(vec![fixture()["sweep"].clone()])
        .await
        .unwrap();
    archive
        .append_observations(vec![fixture()["observation"].clone()])
        .await
        .unwrap();
    (root, archive)
}

#[tokio::test]
async fn selected_records_keep_original_arrays_bits_scope_and_frozen_boundary() {
    let (_root, archive) = seeded().await;
    let mut original = fixture()["sweep"].clone();
    original["id"] = json!("e\u{301}/%+?");
    original["settings"]["é"] = json!(-0.0);
    original["settings"]["e\u{301}"] = json!("distinct");
    archive.append_sweeps(vec![original.clone()]).await.unwrap();
    original = archive
        .get_sweep(original["id"].as_str().unwrap())
        .await
        .unwrap()
        .unwrap();
    let reader = ScopedSnapshotExport::open_live(
        archive.clone(),
        ScopedSnapshotQuery {
            session_id: Some(fixture()["session"]["id"].as_str().unwrap().into()),
            started_at_us: Some(1_794_398_400_123_456),
            ended_at_us: Some(1_794_398_400_123_457),
            min_frequency_hz: Some(100_000_002),
            max_frequency_hz: Some(100_000_002),
            ..Default::default()
        },
        Default::default(),
    )
    .unwrap();
    let info = reader.ready().await.unwrap();
    let mut later = original.clone();
    later["id"] = json!("later-after-boundary");
    archive.append_sweeps(vec![later]).await.unwrap();
    let mut streams = Vec::new();
    let mut sweeps = Vec::new();
    while let Some(record) = reader.next_record().await.unwrap() {
        match record.kind {
            ScopedSnapshotKind::Stream => streams.push(record.value),
            ScopedSnapshotKind::Sweep => sweeps.push(record.value),
            ScopedSnapshotKind::Observation => panic!("unselected receiver frequency"),
        }
    }
    assert_eq!(info.boundary, 4);
    assert_eq!(streams.len(), 2);
    assert!(
        streams
            .iter()
            .all(|s| s["session_id"] == fixture()["session"]["id"])
    );
    assert_eq!(sweeps.len(), 2);
    let selected = sweeps.iter().find(|s| s["id"] == original["id"]).unwrap();
    assert_eq!(selected, &original);
    assert_eq!(selected["levels"].as_array().unwrap().len(), 4);
    assert_eq!(
        selected["levels"][1].as_f64().unwrap().to_bits(),
        (-0.0f64).to_bits()
    );
    assert_eq!(
        selected["levels"][2].as_f64().unwrap().to_bits(),
        (-1.0000000000000002f64).to_bits()
    );
    reader.close().await.unwrap();
    assert!(
        archive
            .get_sweep("later-after-boundary")
            .await
            .unwrap()
            .is_some()
    );
    archive.close().await.unwrap();
}

#[tokio::test]
async fn sparse_frequency_and_half_open_time_selection_do_not_copy_unselected_arrays() {
    let (_root, archive) = seeded().await;
    let mut sparse = fixture()["sweep"].clone();
    sparse["id"] = json!("sparse");
    sparse["frequencies_hz"] = json!([100_000_000, 200_000_000]);
    sparse["levels"] = json!([-0.0, -1.0000000000000002]);
    archive.append_sweeps(vec![sparse]).await.unwrap();
    for query in [
        ScopedSnapshotQuery {
            min_frequency_hz: Some(150_000_000),
            max_frequency_hz: Some(160_000_000),
            ..Default::default()
        },
        ScopedSnapshotQuery {
            ended_at_us: Some(1_794_398_400_123_456),
            ..Default::default()
        },
    ] {
        let reader =
            ScopedSnapshotExport::open_live(archive.clone(), query, Default::default()).unwrap();
        reader.ready().await.unwrap();
        while let Some(record) = reader.next_record().await.unwrap() {
            assert_eq!(record.kind, ScopedSnapshotKind::Stream);
        }
        reader.close().await.unwrap();
    }
    archive.close().await.unwrap();
}

#[tokio::test]
async fn scoped_and_legacy_readers_share_admission_and_archive_close_joins_both() {
    let (root, archive) = seeded().await;
    let old = SnapshotExport::open_live(
        archive.clone(),
        SnapshotExportQuery {
            session_id: fixture()["session"]["id"].as_str().unwrap().into(),
            stream_id: None,
        },
        Default::default(),
    )
    .unwrap();
    let scoped =
        ScopedSnapshotExport::open_live(archive.clone(), Default::default(), Default::default())
            .unwrap();
    old.ready().await.unwrap();
    scoped.ready().await.unwrap();
    assert!(matches!(
        ScopedSnapshotExport::open_live(archive.clone(), Default::default(), Default::default()),
        Err(ArchiveError::QueueFull { .. })
    ));
    archive.close().await.unwrap();
    assert!(matches!(old.next_record().await, Err(ArchiveError::Closed)));
    assert!(matches!(
        scoped.next_record().await,
        Err(ArchiveError::Closed)
    ));
    old.close().await.unwrap();
    scoped.close().await.unwrap();
    let reopened = Archive::open(ArchiveConfig::new(root.path().join("source.sqlite"))).unwrap();
    reopened.close().await.unwrap();
}

#[tokio::test]
async fn absolute_lowered_read_deadline_and_record_budget_fail_closed() {
    let (_root, archive) = seeded().await;
    let reader = ScopedSnapshotExport::open_live(
        archive.clone(),
        Default::default(),
        SnapshotExportLimits {
            read_timeout: Duration::from_nanos(1),
            ..Default::default()
        },
    )
    .unwrap();
    assert!(matches!(
        reader.ready().await,
        Err(ArchiveError::QueryBudget)
    ));
    reader.close().await.unwrap();
    let reader = ScopedSnapshotExport::open_live(
        archive.clone(),
        Default::default(),
        SnapshotExportLimits {
            max_record_bytes: 32,
            ..Default::default()
        },
    )
    .unwrap();
    let failed = match reader.ready().await {
        Err(ArchiveError::QueryBudget) => true,
        Ok(_) => matches!(reader.next_record().await, Err(ArchiveError::QueryBudget)),
        Err(error) => panic!("unexpected reader failure: {error}"),
    };
    assert!(failed);
    reader.close().await.unwrap();
    archive.close().await.unwrap();
}

#[tokio::test]
async fn omitted_session_scope_and_stream_only_scope_preserve_original_observation_bits() {
    let (_root, archive) = seeded().await;
    let mut session = fixture()["session"].clone();
    session["id"] = json!("e\u{301}-session");
    session["created_at"] = json!("2026-11-11T12:00:00.123456Z");
    session["streams"][0]["id"] = json!("other-spectrum");
    session["streams"][1]["id"] = json!("e\u{301}-receiver");
    archive.create_session(session.clone()).await.unwrap();
    let mut observation = fixture()["observation"].clone();
    observation["id"] = json!("e\u{301}-observation");
    observation["session_id"] = session["id"].clone();
    observation["stream_id"] = session["streams"][1]["id"].clone();
    observation["level"] = json!(-0.0);
    observation["metadata"] = json!({"é":-0.0,"e\u{301}":-1.0000000000000002});
    archive
        .append_observations(vec![observation.clone()])
        .await
        .unwrap();
    for (stream, expected_streams, expected_observations) in
        [(None, 4, 2), (Some("e\u{301}-receiver"), 1, 1)]
    {
        let reader = ScopedSnapshotExport::open_live(
            archive.clone(),
            ScopedSnapshotQuery {
                stream_id: stream.map(str::to_owned),
                ..Default::default()
            },
            Default::default(),
        )
        .unwrap();
        reader.ready().await.unwrap();
        let (mut streams, mut observations) = (0, 0);
        while let Some(record) = reader.next_record().await.unwrap() {
            match record.kind {
                ScopedSnapshotKind::Stream => streams += 1,
                ScopedSnapshotKind::Observation => {
                    observations += 1;
                    if record.value["id"] == observation["id"] {
                        assert_eq!(record.value, observation);
                        assert_eq!(
                            record.value["level"].as_f64().unwrap().to_bits(),
                            (-0.0f64).to_bits()
                        );
                        assert_eq!(
                            record.value["metadata"]["e\u{301}"]
                                .as_f64()
                                .unwrap()
                                .to_bits(),
                            (-1.0000000000000002f64).to_bits()
                        );
                    }
                }
                ScopedSnapshotKind::Sweep => assert!(stream.is_none()),
            }
        }
        assert_eq!(
            (streams, observations),
            (expected_streams, expected_observations)
        );
        reader.close().await.unwrap();
    }
    archive.close().await.unwrap();
}
