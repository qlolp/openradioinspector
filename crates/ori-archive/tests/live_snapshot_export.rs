use ori_archive::{
    Archive, ArchiveConfig, ArchiveError, SnapshotExport, SnapshotExportKind, SnapshotExportLimits,
    SnapshotExportQuery,
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
fn query(stream: Option<&str>) -> SnapshotExportQuery {
    SnapshotExportQuery {
        session_id: "fixture-сессия".into(),
        stream_id: stream.map(str::to_owned),
    }
}
async fn seeded() -> (tempfile::TempDir, Archive) {
    let directory = tempfile::tempdir().unwrap();
    let archive = Archive::open(ArchiveConfig::new(directory.path().join("live.sqlite"))).unwrap();
    let mut session = fixture()["session"].clone();
    session["created_at"] = json!("2026-11-11T12:00:00.123456Z");
    archive.create_session(session).await.unwrap();
    let sweeps = ["tie-\u{10000}", "tie-\u{e000}", "tie-é", "tie-e\u{301}"]
        .into_iter()
        .map(|id| {
            let mut value = fixture()["sweep"].clone();
            value["id"] = json!(id);
            value
        })
        .collect();
    archive.append_sweeps(sweeps).await.unwrap();
    let mut observation = fixture()["observation"].clone();
    observation["level"] = json!(-0.0);
    archive
        .append_observations(vec![observation])
        .await
        .unwrap();
    (directory, archive)
}

#[tokio::test]
async fn live_snapshot_freezes_counts_boundary_and_exact_sources_during_new_commits() {
    let (directory, archive) = seeded().await;
    let reader = SnapshotExport::open_live(
        archive.clone(),
        query(None),
        SnapshotExportLimits::default(),
    )
    .unwrap();
    let info = reader.ready().await.unwrap();
    assert_eq!((info.sweeps, info.observations, info.boundary), (4, 1, 6));
    let mut later = fixture()["sweep"].clone();
    later["id"] = json!("committed-after-live-snapshot");
    archive.append_sweeps(vec![later]).await.unwrap();
    let mut ids = Vec::new();
    while let Some(record) = reader.next_record().await.unwrap() {
        let original = if record.kind == SnapshotExportKind::Sweep {
            ids.push(record.value["id"].as_str().unwrap().to_owned());
            fixture()["sweep"].clone()
        } else {
            assert_eq!(
                record.value["level"].as_f64().unwrap().to_bits(),
                (-0.0f64).to_bits()
            );
            assert_eq!(record.value["level_unit"], "s_meter_count");
            fixture()["observation"].clone()
        };
        for key in ["started_at", "ended_at"] {
            let actual = record.value["interval"][key].as_str().unwrap();
            let expected = original["interval"][key].as_str().unwrap();
            assert_eq!(
                chrono::DateTime::parse_from_rfc3339(actual)
                    .unwrap()
                    .timestamp_micros(),
                chrono::DateTime::parse_from_rfc3339(expected)
                    .unwrap()
                    .timestamp_micros()
            );
            assert!(actual.ends_with('Z'));
            assert_eq!(
                actual.split('.').nth(1).unwrap().len(),
                7,
                "six UTC fractional digits"
            );
        }
        if record.kind == SnapshotExportKind::Sweep {
            assert_eq!(record.value["frequencies_hz"], original["frequencies_hz"]);
            assert_eq!(record.value["settings"], original["settings"]);
            for (actual, expected) in record.value["levels"]
                .as_array()
                .unwrap()
                .iter()
                .zip(original["levels"].as_array().unwrap())
            {
                assert_eq!(
                    actual.as_f64().unwrap().to_bits(),
                    expected.as_f64().unwrap().to_bits()
                );
            }
        }
    }
    assert_eq!(
        ids,
        ["tie-e\u{301}", "tie-é", "tie-\u{e000}", "tie-\u{10000}"]
    );
    assert!(reader.next_record().await.unwrap().is_none());
    reader.close().await.unwrap();
    assert_eq!(archive.events(0, 100).await.unwrap().len(), 7);
    assert!(
        !directory.path().join("snapshot").exists(),
        "live export must not copy the archive"
    );
    archive.close().await.unwrap();
    let recovered =
        Archive::open(ArchiveConfig::new(directory.path().join("live.sqlite"))).unwrap();
    assert!(
        recovered
            .get_sweep("committed-after-live-snapshot")
            .await
            .unwrap()
            .is_some()
    );
    recovered.close().await.unwrap();
}

#[tokio::test]
async fn two_live_readers_are_bounded_until_actual_join_then_capacity_is_reusable() {
    let (_directory, archive) = seeded().await;
    let a = SnapshotExport::open_live(archive.clone(), query(None), Default::default()).unwrap();
    let b = SnapshotExport::open_live(archive.clone(), query(None), Default::default()).unwrap();
    a.ready().await.unwrap();
    b.ready().await.unwrap();
    assert!(matches!(
        SnapshotExport::open_live(archive.clone(), query(None), Default::default()),
        Err(ArchiveError::QueueFull { .. })
    ));
    a.close().await.unwrap();
    let c = SnapshotExport::open_live(archive.clone(), query(None), Default::default()).unwrap();
    c.ready().await.unwrap();
    assert!(a.ready().await.is_err());
    b.close().await.unwrap();
    c.close().await.unwrap();
    archive.close().await.unwrap();
}

#[tokio::test]
async fn archive_close_joins_idle_live_readers_even_while_callers_keep_handles() {
    let (directory, archive) = seeded().await;
    let reader =
        SnapshotExport::open_live(archive.clone(), query(None), Default::default()).unwrap();
    reader.ready().await.unwrap();
    tokio::time::timeout(Duration::from_secs(2), archive.close())
        .await
        .unwrap()
        .unwrap();
    assert!(matches!(reader.ready().await, Err(ArchiveError::Closed)));
    assert!(matches!(
        reader.next_record().await,
        Err(ArchiveError::Closed)
    ));
    reader.close().await.unwrap();
    assert!(matches!(
        SnapshotExport::open_live(archive.clone(), query(None), Default::default()),
        Err(ArchiveError::Closed)
    ));
    let reopened = Archive::open(ArchiveConfig::new(directory.path().join("live.sqlite"))).unwrap();
    reopened.close().await.unwrap();
}

#[tokio::test]
async fn missing_and_foreign_context_and_lowered_limits_never_mutate_live_sources() {
    let (_directory, archive) = seeded().await;
    for (session, stream) in [("absent", None), ("fixture-сессия", Some("foreign"))] {
        let mut q = query(stream);
        q.session_id = session.into();
        let reader = SnapshotExport::open_live(archive.clone(), q, Default::default()).unwrap();
        let error = reader.ready().await.unwrap_err();
        assert!(if session == "absent" {
            matches!(error, ArchiveError::NotFound)
        } else {
            matches!(error, ArchiveError::Invalid(_))
        });
        reader.close().await.unwrap();
    }
    let reader = SnapshotExport::open_live(
        archive.clone(),
        query(None),
        SnapshotExportLimits {
            max_record_bytes: 128,
            ..Default::default()
        },
    )
    .unwrap();
    reader.ready().await.unwrap();
    assert!(matches!(
        reader.next_record().await,
        Err(ArchiveError::QueryBudget)
    ));
    assert!(matches!(
        reader.next_record().await,
        Err(ArchiveError::QueryBudget)
    ));
    reader.close().await.unwrap();
    assert_eq!(archive.events(0, 100).await.unwrap().len(), 6);
    archive.close().await.unwrap();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn start_close_races_cannot_open_a_reader_after_writer_release() {
    for _ in 0..16 {
        let (directory, archive) = seeded().await;
        let starting = archive.clone();
        let task = tokio::task::spawn_blocking(move || {
            SnapshotExport::open_live(starting, query(None), Default::default())
        });
        archive.close().await.unwrap();
        match task.await.unwrap() {
            Ok(reader) => {
                assert!(matches!(reader.ready().await, Err(ArchiveError::Closed)));
                reader.close().await.unwrap();
            }
            Err(ArchiveError::Closed) => {}
            Err(error) => panic!("unexpected admission result: {error:?}"),
        }
        let reopened =
            Archive::open(ArchiveConfig::new(directory.path().join("live.sqlite"))).unwrap();
        reopened.close().await.unwrap();
    }
}
