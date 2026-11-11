use ori_archive::{Archive, ArchiveConfig, MonitoringQuery, SweepQuery};
use serde_json::json;

fn legacy() -> (tempfile::TempDir, std::path::PathBuf) {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("archive.sqlite");
    std::fs::copy(
        concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/tests/fixtures/legacy_v5_utf16.sqlite"
        ),
        &path,
    )
    .unwrap();
    (dir, path)
}

#[tokio::test]
async fn bounded_utf16_history_and_context_use_canonical_utc_and_exact_utf8_id_ties() {
    let (_dir, path) = legacy();
    let archive = Archive::open(ArchiveConfig::new(path)).unwrap();
    let mut query = SweepQuery::session("fixture-сессия");
    query.limit = 2;
    let first = archive.sweep_page(query.clone()).await.unwrap();
    assert_eq!(
        first
            .items
            .iter()
            .map(|v| v["id"].as_str().unwrap())
            .collect::<Vec<_>>(),
        vec!["tie-\u{10000}", "tie-\u{e000}"]
    );
    query.after = first.next_cursor;
    let second = archive.sweep_page(query).await.unwrap();
    assert_eq!(
        second
            .items
            .iter()
            .map(|v| v["id"].as_str().unwrap())
            .collect::<Vec<_>>(),
        vec!["arrival-last", "arrival-first"]
    );
    assert!(second.next_cursor.is_none());
    let context = archive
        .sweep_context("fixture-сессия", "tie-\u{e000}", 2, 2)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(
        context
            .before
            .iter()
            .map(|v| v["id"].as_str().unwrap())
            .collect::<Vec<_>>(),
        vec!["arrival-first", "arrival-last"]
    );
    assert_eq!(context.after[0]["id"], "tie-\u{10000}");
    assert!(
        archive
            .sweep_context("fixture-сессия", "absent", 1, 1)
            .await
            .unwrap()
            .is_none()
    );
    assert!(
        archive
            .sweep_context("fixture-сессия", "arrival-first", 17, 1)
            .await
            .is_err()
    );
    let latest = archive
        .list_sweeps("fixture-сессия", Some("spectrum/one"), 2)
        .await
        .unwrap();
    assert_eq!(
        latest
            .iter()
            .map(|v| v["id"].as_str().unwrap())
            .collect::<Vec<_>>(),
        vec!["tie-\u{e000}", "tie-\u{10000}"]
    );
    let owned = archive
        .measurement_owners(vec![
            "arrival-last".into(),
            "fixture-observation-α".into(),
            "missing".into(),
        ])
        .await
        .unwrap();
    assert_eq!(
        owned["arrival-last"],
        vec![("fixture-сессия".to_owned(), "spectrum/one".to_owned())]
    );
    assert!(owned["missing"].is_empty());
    archive.close().await.unwrap();
}

#[tokio::test]
async fn ingestion_cursor_is_capture_order_and_pages_do_not_follow_timestamps() {
    let (_dir, path) = legacy();
    let archive = Archive::open(ArchiveConfig::new(path)).unwrap();
    let initial = archive
        .sweep_ingestion_boundary("fixture-сессия")
        .await
        .unwrap();
    assert_eq!(initial, 4);
    let mut source = archive.get_sweep("arrival-last").await.unwrap().unwrap();
    source["id"] = json!("earlier-time-later-arrival");
    source["interval"] =
        json!({"started_at":"2026-01-01T00:00:00Z","ended_at":"2026-01-01T00:00:01Z"});
    archive.append_sweeps(vec![source]).await.unwrap();
    let pending = archive
        .sweeps_after_ingestion("fixture-сессия", Some("spectrum/one"), initial, 1)
        .await
        .unwrap();
    assert_eq!(pending.len(), 1);
    assert_eq!(pending[0].0, 5);
    assert_eq!(pending[0].1["id"], "earlier-time-later-arrival");
    assert!(
        archive
            .sweeps_after_ingestion("fixture-сессия", None, 5, 1)
            .await
            .unwrap()
            .is_empty()
    );
    archive.close().await.unwrap();
}

#[tokio::test]
async fn monitoring_payload_updates_preserve_history_columns_and_memberships() {
    let (_dir, path) = legacy();
    let archive = Archive::open(ArchiveConfig::new(&path)).unwrap();
    let mut original = archive
        .monitoring("project", "synthetic-project")
        .await
        .unwrap()
        .unwrap();
    original["created_at"] = json!("2026-12-01T00:00:00Z");
    original["name"] = json!("Changed view");
    archive
        .put_monitoring("project", original.clone())
        .await
        .unwrap();
    assert_eq!(
        archive
            .monitoring("project", "synthetic-project")
            .await
            .unwrap()
            .unwrap(),
        original
    );
    assert!(
        archive
            .monitoring_member("synthetic-project", "fixture-сессия")
            .await
            .unwrap()
    );
    assert!(
        !archive
            .monitoring_member("other", "fixture-сессия")
            .await
            .unwrap()
    );
    let list = archive
        .list_monitoring(
            "project",
            MonitoringQuery {
                limit: 1,
                ..Default::default()
            },
        )
        .await
        .unwrap();
    assert_eq!(list.len(), 1);
    assert_eq!(list[0]["id"], "synthetic-project");
    assert!(
        archive
            .list_monitoring("arbitrary_sql", MonitoringQuery::default())
            .await
            .is_err()
    );
    let db = rusqlite::Connection::open(path).unwrap();
    let created: String = db
        .query_row(
            "SELECT created_at FROM monitoring_records WHERE kind='project'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(created, "2026-11-11T12:00:00.123456Z");
    archive.close().await.unwrap();
}

#[tokio::test]
async fn bounded_sessions_counts_capacity_and_summary_retain_units_and_first_peak_tie() {
    let (_dir, path) = legacy();
    let archive = Archive::open(ArchiveConfig::new(path)).unwrap();
    let page = archive.list_sessions(1, None).await.unwrap();
    assert_eq!(page.items.len(), 1);
    assert!(page.next_cursor.is_none());
    assert_eq!(page.items[0]["streams"][0]["id"], "spectrum/one");
    let summary = archive
        .analytics_summary("fixture-сессия", None)
        .await
        .unwrap();
    assert_eq!(summary["sweeps"]["count"], 4);
    assert_eq!(summary["observations"]["count"], 1);
    assert_eq!(summary["sweeps"]["latest_peak"]["frequency_hz"], 100000002);
    assert_eq!(
        summary["observations"]["latest"]["level_unit"],
        "s_meter_count"
    );
    assert_eq!(summary["observations"]["squelch_open_ratio"], 1.0);
    let capacity = archive.capacity().await.unwrap();
    assert_eq!(capacity["state"], "unlimited");
    assert_eq!(capacity["automatic_deletion"], false);
    let counts = archive.registry_counts().await.unwrap();
    assert_eq!(counts["sessions"], 1);
    assert_eq!(counts["last_event_id"], 0);
    assert_eq!(archive.status().await.unwrap()["sqlite_version"], "3.54.1");
    let observations = archive
        .list_observations("fixture-сессия", Some("receiver+one"), 1)
        .await
        .unwrap();
    assert_eq!(observations[0]["frequency_hz"], 100000003);
    assert!(archive.list_sessions(1001, None).await.is_err());
    assert!(
        archive
            .list_sweeps("fixture-сессия", None, 0)
            .await
            .is_err()
    );
    archive.close().await.unwrap();
}

#[tokio::test]
async fn summaries_freeze_ingestion_half_open_time_and_whole_source_first_peak_without_raw_arrays()
{
    let (_dir, path) = legacy();
    let archive = Archive::open(ArchiveConfig::new(path)).unwrap();
    let boundary = archive
        .sweep_ingestion_boundary("fixture-сессия")
        .await
        .unwrap();
    let mut query = SweepQuery::session("fixture-сессия");
    query.limit = 1;
    query.min_hz = Some(100000004);
    let page = archive
        .sweep_summaries(query.clone(), boundary)
        .await
        .unwrap();
    assert_eq!(page.items[0]["id"], "tie-\u{10000}");
    assert_eq!(page.items[0]["peak_frequency_hz"], 100000002);
    assert_eq!(page.items[0]["point_count"], 4);
    assert!(page.items[0].get("levels").is_none());
    let stamp = page.items[0]["interval"]["started_at"]
        .as_str()
        .unwrap()
        .to_owned();
    let mut late = archive.get_sweep("arrival-last").await.unwrap().unwrap();
    late["id"] = json!("tie-\u{f000}");
    late["interval"] = page.items[0]["interval"].clone();
    archive.append_sweeps(vec![late]).await.unwrap();
    query.after = page.next_cursor;
    let next = archive
        .sweep_summaries(query.clone(), boundary)
        .await
        .unwrap();
    assert_eq!(next.items[0]["id"], "tie-\u{e000}");
    query.after = None;
    query.limit = 100;
    query.end = Some(stamp);
    let past = archive.sweep_summaries(query, boundary).await.unwrap();
    assert!(past.items.is_empty());
    assert!(
        archive
            .sweep_summaries(SweepQuery::session("fixture-сессия"), 999)
            .await
            .is_err()
    );
    archive.close().await.unwrap();
}
