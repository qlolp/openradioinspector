use ori_archive::{
    Archive, ArchiveConfig, ArchiveError, SnapshotExport, SnapshotExportKind, SnapshotExportLimits,
    SnapshotExportQuery,
};
use serde_json::{Value, json};
use std::{future::Future, task::Poll, time::Duration};

fn fixture() -> Value {
    ori_archive::parse_json_value(
        include_bytes!("../../../contracts/fixtures/measurement-v1.json"),
        1024 * 1024,
    )
    .unwrap()
}
async fn seeded() -> (tempfile::TempDir, Archive) {
    let directory = tempfile::tempdir().unwrap();
    let archive = Archive::open(ArchiveConfig::new(directory.path().join("live.sqlite"))).unwrap();
    let mut session = fixture()["session"].clone();
    session["created_at"] = json!("2026-11-11T12:00:00.123456Z");
    archive.create_session(session).await.unwrap();
    let mut sweeps = Vec::new();
    for id in ["tie-\u{10000}", "tie-\u{e000}", "tie-é", "tie-e\u{301}"] {
        let mut value = fixture()["sweep"].clone();
        value["id"] = json!(id);
        sweeps.push(value);
    }
    archive.append_sweeps(sweeps).await.unwrap();
    let mut observation = fixture()["observation"].clone();
    observation["level"] = json!(-0.0);
    archive
        .append_observations(vec![observation])
        .await
        .unwrap();
    (directory, archive)
}
async fn snapshot(archive: &Archive) -> tempfile::TempDir {
    let mut builder = tempfile::Builder::new();
    builder.prefix("ori-export-snapshot-");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        builder.permissions(std::fs::Permissions::from_mode(0o700));
    }
    let root = builder.tempdir().unwrap();
    archive.backup(&root.path().join("snapshot")).await.unwrap();
    root
}
fn query(stream: Option<&str>) -> SnapshotExportQuery {
    SnapshotExportQuery {
        session_id: "fixture-сессия".into(),
        stream_id: stream.map(str::to_owned),
    }
}

#[tokio::test]
async fn sealed_counts_and_original_values_are_stable_during_new_commits() {
    let (_live, archive) = seeded().await;
    let root = snapshot(&archive).await;
    let path = root.path().to_owned();
    let before = std::fs::read(path.join("snapshot/archive.sqlite")).unwrap();
    let reader = SnapshotExport::open(root, query(None), SnapshotExportLimits::default()).unwrap();
    let info = reader.ready().await.unwrap();
    assert_eq!((info.sweeps, info.observations, info.boundary), (4, 1, 6));
    assert_eq!(info.session_id, "fixture-сессия");
    let mut late = fixture()["sweep"].clone();
    late["id"] = json!("new-after-snapshot");
    archive.append_sweeps(vec![late]).await.unwrap();
    let mut ids = Vec::new();
    while let Some(record) = reader.next_record().await.unwrap() {
        if record.kind == SnapshotExportKind::Sweep {
            ids.push(record.value["id"].as_str().unwrap().to_owned());
            assert_eq!(
                record.value["interval"]["started_at"],
                "2026-11-11T12:00:00.123456Z"
            );
            let original = &fixture()["sweep"];
            assert_eq!(record.value["frequencies_hz"], original["frequencies_hz"]);
            for (a, b) in record.value["levels"]
                .as_array()
                .unwrap()
                .iter()
                .zip(original["levels"].as_array().unwrap())
            {
                assert_eq!(a.as_f64().unwrap().to_bits(), b.as_f64().unwrap().to_bits());
            }
        } else {
            assert_eq!(
                record.value["level"].as_f64().unwrap().to_bits(),
                (-0.0f64).to_bits()
            );
            assert_eq!(record.value["level_unit"], "s_meter_count");
        }
    }
    assert_eq!(
        ids,
        ["tie-e\u{301}", "tie-é", "tie-\u{e000}", "tie-\u{10000}"]
    );
    assert!(reader.next_record().await.unwrap().is_none());
    assert_eq!(
        std::fs::read(path.join("snapshot/archive.sqlite")).unwrap(),
        before
    );
    assert!(!path.join("snapshot/archive.sqlite-wal").exists());
    reader.close().await.unwrap();
    assert!(
        !path.exists(),
        "close must join the reader and release its owned stage"
    );
    assert!(matches!(
        reader.next_record().await,
        Err(ArchiveError::Closed)
    ));
    assert_eq!(archive.events(0, 100).await.unwrap().len(), 7);
    archive.close().await.unwrap();
}

#[tokio::test]
async fn stream_filters_and_missing_context_are_distinct_without_source_mutation() {
    let (_live, archive) = seeded().await;
    let stream = fixture()["session"]["streams"][1]["id"]
        .as_str()
        .unwrap()
        .to_owned();
    let reader = SnapshotExport::open(
        snapshot(&archive).await,
        query(Some(&stream)),
        SnapshotExportLimits::default(),
    )
    .unwrap();
    let info = reader.ready().await.unwrap();
    assert_eq!((info.sweeps, info.observations), (0, 1));
    assert_eq!(
        reader.next_record().await.unwrap().unwrap().kind,
        SnapshotExportKind::Observation
    );
    assert!(reader.next_record().await.unwrap().is_none());
    reader.close().await.unwrap();
    for (session, stream) in [("absent", None), ("fixture-сессия", Some("foreign"))] {
        let mut q = query(stream);
        q.session_id = session.into();
        let reader =
            SnapshotExport::open(snapshot(&archive).await, q, SnapshotExportLimits::default())
                .unwrap();
        let error = reader.ready().await.unwrap_err();
        if session == "absent" {
            assert!(matches!(error, ArchiveError::NotFound));
        } else {
            assert!(matches!(error, ArchiveError::Invalid(_)));
        }
        reader.close().await.unwrap();
    }
    assert_eq!(archive.events(0, 100).await.unwrap().len(), 6);
    archive.close().await.unwrap();
}

#[tokio::test]
async fn lowering_record_limit_fails_explicitly_and_closes_without_truncation() {
    let (_live, archive) = seeded().await;
    let root = snapshot(&archive).await;
    let path = root.path().to_owned();
    let reader = SnapshotExport::open(
        root,
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
    assert!(!path.exists());
    assert_eq!(archive.events(0, 100).await.unwrap().len(), 6);
    archive.close().await.unwrap();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn cancelled_close_still_joins_a_blocked_reader_before_removing_the_stage() {
    let (_live, archive) = seeded().await;
    let root = snapshot(&archive).await;
    let path = root.path().to_owned();
    let blocker = rusqlite::Connection::open(path.join("snapshot/archive.sqlite")).unwrap();
    blocker.execute_batch("BEGIN EXCLUSIVE").unwrap();
    let reader = SnapshotExport::open(
        root,
        query(None),
        SnapshotExportLimits {
            read_timeout: Duration::from_millis(500),
            ..Default::default()
        },
    )
    .unwrap();
    assert!(
        tokio::time::timeout(Duration::from_millis(20), reader.ready())
            .await
            .is_err()
    );
    let mut close = Box::pin(reader.close());
    std::future::poll_fn(|context| match close.as_mut().poll(context) {
        Poll::Pending => Poll::Ready(()),
        Poll::Ready(value) => panic!("close returned before the blocked reader joined: {value:?}"),
    })
    .await;
    drop(close);
    blocker.execute_batch("ROLLBACK").unwrap();
    drop(blocker);
    reader.close().await.unwrap();
    assert!(!path.exists());
    assert!(matches!(reader.ready().await, Err(ArchiveError::Closed)));
    archive.close().await.unwrap();
}

#[tokio::test]
async fn dropping_an_unconsumed_reader_releases_only_its_owned_snapshot() {
    let (_live, archive) = seeded().await;
    let root = snapshot(&archive).await;
    let path = root.path().to_owned();
    let reader = SnapshotExport::open(root, query(None), SnapshotExportLimits::default()).unwrap();
    reader.ready().await.unwrap();
    drop(reader);
    tokio::time::timeout(Duration::from_secs(2), async {
        while path.exists() {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("the owned reaper must remove its stage after last Drop");
    assert!(archive.get_sweep("tie-é").await.unwrap().is_some());
    archive.close().await.unwrap();
}

#[tokio::test]
async fn malformed_bundle_and_invalid_budgets_cannot_open_or_rewrite_other_files() {
    let (_live, archive) = seeded().await;
    let root = snapshot(&archive).await;
    let path = root.path().to_owned();
    let manifest = path.join("snapshot/manifest.json");
    std::fs::write(&manifest, b"{\"backup_version\":1,\"backup_version\":2}").unwrap();
    let reader = SnapshotExport::open(root, query(None), SnapshotExportLimits::default()).unwrap();
    assert!(matches!(
        reader.ready().await,
        Err(ArchiveError::Corrupt(_))
    ));
    reader.close().await.unwrap();
    assert!(!path.exists());
    for limits in [
        SnapshotExportLimits {
            max_record_bytes: 0,
            ..Default::default()
        },
        SnapshotExportLimits {
            max_record_bytes: 16 * 1024 * 1024 + 1,
            ..Default::default()
        },
        SnapshotExportLimits {
            read_timeout: Duration::ZERO,
            ..Default::default()
        },
        SnapshotExportLimits {
            read_timeout: Duration::from_secs(6),
            ..Default::default()
        },
    ] {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().to_owned();
        assert!(matches!(
            SnapshotExport::open(root, query(None), limits),
            Err(ArchiveError::Invalid(_))
        ));
        assert!(!path.exists());
    }
    assert_eq!(archive.events(0, 100).await.unwrap().len(), 6);
    archive.close().await.unwrap();
}

#[cfg(unix)]
#[tokio::test]
async fn nonprivate_snapshot_directories_are_rejected_without_changing_the_live_archive() {
    use std::os::unix::fs::PermissionsExt;

    let (_live, archive) = seeded().await;
    for public_directory in ["", "snapshot"] {
        let root = snapshot(&archive).await;
        let path = root.path().to_owned();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o700)).unwrap();
        std::fs::set_permissions(
            path.join(public_directory),
            std::fs::Permissions::from_mode(0o755),
        )
        .unwrap();
        let reader =
            SnapshotExport::open(root, query(None), SnapshotExportLimits::default()).unwrap();
        assert!(matches!(
            reader.ready().await,
            Err(ArchiveError::Corrupt(_))
        ));
        reader.close().await.unwrap();
        assert!(
            !path.exists(),
            "rejection must still join its owned cleanup"
        );
    }
    assert_eq!(archive.events(0, 100).await.unwrap().len(), 6);
    let original = fixture()["sweep"].clone();
    let actual = archive.get_sweep("tie-é").await.unwrap().unwrap();
    assert_eq!(actual["frequencies_hz"], original["frequencies_hz"]);
    assert_eq!(actual["levels"], original["levels"]);
    archive.close().await.unwrap();
}

#[tokio::test]
async fn an_empty_snapshot_has_a_header_and_repeatable_eof() {
    let dir = tempfile::tempdir().unwrap();
    let archive = Archive::open(ArchiveConfig::new(dir.path().join("empty.sqlite"))).unwrap();
    let mut session = fixture()["session"].clone();
    session["created_at"] = json!("2026-11-11T12:00:00Z");
    archive.create_session(session).await.unwrap();
    let reader = SnapshotExport::open(
        snapshot(&archive).await,
        query(None),
        SnapshotExportLimits::default(),
    )
    .unwrap();
    let info = reader.ready().await.unwrap();
    assert_eq!((info.sweeps, info.observations, info.boundary), (0, 0, 1));
    assert!(reader.next_record().await.unwrap().is_none());
    assert!(reader.next_record().await.unwrap().is_none());
    reader.close().await.unwrap();
    archive.close().await.unwrap();
}

#[tokio::test]
async fn existing_long_ids_and_control_characters_keep_the_archive_contract() {
    let directory = tempfile::tempdir().unwrap();
    let archive = Archive::open(ArchiveConfig::new(directory.path().join("long.sqlite"))).unwrap();
    let session_id = format!("session-\n{}", "я".repeat(2000));
    let stream_id = format!("stream-\0{}", "x".repeat(2000));
    let mut session = fixture()["session"].clone();
    session["created_at"] = json!("2026-11-11T12:00:00Z");
    session["id"] = json!(session_id);
    let mut stream = session["streams"][0].clone();
    stream["id"] = json!(stream_id);
    stream["session_id"] = json!(session_id);
    session["streams"] = json!([stream]);
    archive.create_session(session).await.unwrap();
    let mut sweep = fixture()["sweep"].clone();
    sweep["session_id"] = json!(session_id);
    sweep["stream_id"] = json!(stream_id);
    archive.append_sweeps(vec![sweep]).await.unwrap();
    let reader = SnapshotExport::open(
        snapshot(&archive).await,
        SnapshotExportQuery {
            session_id: session_id.clone(),
            stream_id: Some(stream_id.clone()),
        },
        SnapshotExportLimits::default(),
    )
    .unwrap();
    let info = reader.ready().await.unwrap();
    assert_eq!(info.session_id, session_id);
    assert_eq!(info.stream_id, Some(stream_id.clone()));
    let value = reader.next_record().await.unwrap().unwrap().value;
    assert_eq!(value["session_id"], session_id);
    assert_eq!(value["stream_id"], stream_id);
    reader.close().await.unwrap();
    archive.close().await.unwrap();
}

#[tokio::test]
async fn cleanup_failure_is_sticky_and_cannot_be_acknowledged_as_closed_successfully() {
    let (_live, archive) = seeded().await;
    let outer = tempfile::tempdir().unwrap();
    let root = tempfile::tempdir_in(outer.path()).unwrap();
    archive.backup(&root.path().join("snapshot")).await.unwrap();
    let original = root.path().to_owned();
    let moved = outer.path().join("still-owned-backup");
    std::fs::rename(&original, &moved).unwrap();
    std::fs::write(&original, b"owned replacement must not be silently removed").unwrap();
    let reader = SnapshotExport::open(root, query(None), SnapshotExportLimits::default()).unwrap();
    assert!(reader.ready().await.is_err());
    assert!(
        reader.close().await.is_err(),
        "failed snapshot removal must retain a failure latch"
    );
    assert!(
        reader.close().await.is_err(),
        "a repeated waiter must observe the same failure"
    );
    assert_eq!(
        std::fs::read(&original).unwrap(),
        b"owned replacement must not be silently removed"
    );
    assert!(moved.join("snapshot/archive.sqlite").is_file());
    archive.close().await.unwrap();
}
