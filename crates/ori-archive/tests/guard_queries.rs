use ori_archive::{Archive, ArchiveConfig, ArchiveError};
use serde_json::Value;

fn fixture(encoding: &str) -> (tempfile::TempDir, std::path::PathBuf) {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("guard.sqlite");
    let name = format!(
        "{}/tests/fixtures/guard_selection_{}.sqlite",
        env!("CARGO_MANIFEST_DIR"),
        encoding.to_lowercase()
    );
    std::fs::copy(name, &path).unwrap();
    (dir, path)
}

fn oracle() -> Value {
    serde_json::from_str(include_str!("fixtures/guard_selection_oracle.json")).unwrap()
}

type SourceRow = (
    i64,
    String,
    String,
    String,
    String,
    String,
    String,
    String,
    String,
);
fn source_rows(path: &std::path::Path) -> Vec<SourceRow> {
    let db =
        rusqlite::Connection::open_with_flags(path, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)
            .unwrap();
    db.prepare("SELECT rowid,id,session_id,stream_id,started_at,ended_at,frequencies_json,levels_json,settings_json FROM sweeps ORDER BY rowid").unwrap()
        .query_map([], |row| Ok((row.get(0)?,row.get(1)?,row.get(2)?,row.get(3)?,row.get(4)?,row.get(5)?,row.get(6)?,row.get(7)?,row.get(8)?)))
        .unwrap().collect::<std::result::Result<Vec<_>,_>>().unwrap()
}

#[tokio::test]
async fn python_sampler_count_span_cadence_and_real_rowid_match_in_both_sqlite_encodings() {
    for encoding in ["UTF-8", "UTF-16"] {
        let (_dir, path) = fixture(encoding);
        let original = source_rows(&path);
        let archive = Archive::open(ArchiveConfig::new(&path)).unwrap();
        let expected = &oracle()[encoding];
        for case in expected["cases"].as_array().unwrap() {
            let session = case["session_id"].as_str().unwrap();
            let stream = case["stream_id"].as_str().unwrap();
            let selected = archive.baseline_selection(session, stream).await.unwrap();
            assert!(selected.sweep_ids.len() <= 256);
            assert_eq!(
                serde_json::to_value(&selected).unwrap(),
                case["selection"],
                "{encoding}/{session}"
            );
            let captures = archive
                .capture_positions(&selected.sweep_ids)
                .await
                .unwrap();
            assert_eq!(captures.len(), selected.sweep_ids.len());
            let pending = archive
                .legacy_pending_sweeps(session, stream, 0, 64)
                .await
                .unwrap();
            assert_eq!(
                serde_json::to_value(&pending).unwrap(),
                case["pending"],
                "{encoding}/{session}"
            );
            if let Some(first) = pending.first() {
                let next = archive
                    .legacy_pending_sweeps(session, stream, first.rowid, 1)
                    .await
                    .unwrap();
                assert_eq!(next.first(), pending.get(1));
            }
            if selected.count != 0 {
                let db = rusqlite::Connection::open_with_flags(
                    &path,
                    rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
                )
                .unwrap();
                let sql = format!(
                    "SELECT MAX(rowid) FROM sweeps WHERE id IN ({})",
                    vec!["?"; selected.sweep_ids.len()].join(",")
                );
                let maximum: i64 = db
                    .query_row(
                        &sql,
                        rusqlite::params_from_iter(selected.sweep_ids.iter()),
                        |row| row.get(0),
                    )
                    .unwrap();
                assert_eq!(
                    captures.values().map(|x| x.rowid).max(),
                    Some(maximum as u64)
                );
            }
        }
        let ids: Vec<String> = expected["position_ids"]
            .as_array()
            .unwrap()
            .iter()
            .map(|x| x.as_str().unwrap().to_owned())
            .collect();
        let positions = archive.capture_positions(&ids).await.unwrap();
        assert_eq!(
            serde_json::to_value(&positions).unwrap(),
            expected["positions"]
        );
        let db = rusqlite::Connection::open_with_flags(
            &path,
            rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
        )
        .unwrap();
        assert!(db.query_row("SELECT EXISTS(SELECT 1 FROM sweeps s JOIN sweep_ingestion i ON i.sweep_id=s.id WHERE s.rowid!=i.sequence)", [], |row| row.get::<_, bool>(0)).unwrap());
        assert!(
            db.query_row("SELECT MAX(rowid)>COUNT(*) FROM sweeps", [], |row| row
                .get::<_, bool>(0))
                .unwrap()
        );
        archive.close().await.unwrap();
        assert_eq!(source_rows(&path), original);
    }
}

#[tokio::test]
async fn selectors_return_only_compact_metadata_and_respect_count_cursor_and_byte_limits() {
    let (_dir, path) = fixture("UTF-8");
    {
        let db = rusqlite::Connection::open(&path).unwrap();
        db.execute("UPDATE sweeps SET levels_json='synthetic unreadable raw array' WHERE session_id='selection/high'", []).unwrap();
    }
    let archive = Archive::open(ArchiveConfig::new(&path)).unwrap();
    let selected = archive
        .baseline_selection("selection/high", "analyzer/high")
        .await
        .unwrap();
    assert!(selected.count >= 700);
    assert_eq!(
        serde_json::to_value(&selected).unwrap(),
        oracle()["UTF-8"]["cases"][3]["selection"]
    );
    assert!(archive.get_sweep(&selected.sweep_ids[0]).await.is_err());
    assert!(matches!(
        archive.capture_positions(&vec!["x".into(); 257]).await,
        Err(ArchiveError::Invalid(_))
    ));
    for (cursor, limit) in [(0, 0), (0, 65), (i64::MAX as u64 + 1, 1)] {
        assert!(matches!(
            archive
                .legacy_pending_sweeps("selection/high", "analyzer/high", cursor, limit)
                .await,
            Err(ArchiveError::Invalid(_))
        ));
    }
    assert!(
        archive
            .legacy_pending_sweeps("selection/high", "foreign", 0, 64)
            .await
            .unwrap()
            .is_empty()
    );
    assert!(archive.capture_positions(&[]).await.unwrap().is_empty());
    archive.close().await.unwrap();
    let mut config = ArchiveConfig::new(path);
    config.max_response_bytes = 1024;
    let archive = Archive::open(config).unwrap();
    assert!(matches!(
        archive
            .baseline_selection("selection/high", "analyzer/high")
            .await,
        Err(ArchiveError::QueryBudget)
    ));
    archive.close().await.unwrap();
}

#[tokio::test]
async fn scoped_open_pending_and_evidence_pages_match_the_actual_python_store() {
    for encoding in ["UTF-8", "UTF-16"] {
        let (_dir, path) = fixture(encoding);
        let archive = Archive::open(ArchiveConfig::new(path)).unwrap();
        let expected = &oracle()[encoding]["events"];
        let opens = archive
            .open_events("guard-project", "selection/high", "baseline-A")
            .await
            .unwrap();
        let pending = archive
            .pending_receiver_events("guard-project", "selection/high")
            .await
            .unwrap();
        assert_eq!(
            serde_json::to_value(opens.iter().map(|x| &x["id"]).collect::<Vec<_>>()).unwrap(),
            expected["open_ids"]
        );
        assert_eq!(
            serde_json::to_value(pending.iter().map(|x| &x["id"]).collect::<Vec<_>>()).unwrap(),
            expected["pending_ids"]
        );
        assert!(opens.len() <= 1000 && pending.len() <= 1);
        assert!(
            archive
                .open_events("guard-project", "foreign", "baseline-A")
                .await
                .unwrap()
                .is_empty()
        );
        assert!(
            archive
                .pending_receiver_events("guard-project", "absent-session")
                .await
                .unwrap()
                .is_empty()
        );
        let mut cursor = String::new();
        let mut ids = Vec::new();
        loop {
            let page = archive
                .event_sources("guard-project", "event-0000", &cursor, 64)
                .await
                .unwrap();
            assert!(page.items.len() <= 64);
            ids.extend(
                page.items
                    .into_iter()
                    .map(|value| value["id"].as_str().unwrap().to_owned()),
            );
            let Some(next) = page.next_cursor else { break };
            cursor = next;
        }
        assert_eq!(
            serde_json::to_value(ids).unwrap(),
            expected["event_source_ids"]
        );
        assert!(matches!(
            archive
                .event_sources("foreign-project", "event-0000", "", 64)
                .await,
            Err(ArchiveError::NotFound)
        ));
        assert!(matches!(
            archive
                .event_sources("guard-project", "absent", "", 64)
                .await,
            Err(ArchiveError::NotFound)
        ));
        for limit in [0, 65] {
            assert!(matches!(
                archive
                    .event_sources("guard-project", "event-0000", "", limit)
                    .await,
                Err(ArchiveError::Invalid(_))
            ));
        }
        archive.close().await.unwrap();
    }
}
