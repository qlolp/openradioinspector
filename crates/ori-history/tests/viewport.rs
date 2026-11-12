#![cfg(unix)]
use ori_archive::{Archive, ArchiveConfig};
use ori_history::{HistoryConfig, HistoryError, HistoryService, ViewportCursor, ViewportQuery};
use ori_runtime::worker::{ResearchPool, WorkerSpec};
use serde_json::{Value, json};
use std::collections::BTreeMap;

fn fixture() -> Value {
    serde_json::from_str(include_str!(
        "../../../contracts/fixtures/measurement-v1.json"
    ))
    .unwrap()
}
fn session() -> Value {
    let mut value = fixture()["session"].clone();
    value["created_at"] = json!("2026-11-11T12:00:00.123456Z");
    value
}
fn spec() -> WorkerSpec {
    WorkerSpec {
        program: std::env::var_os("ORI_RESEARCH_PYTHON")
            .map(Into::into)
            .expect("Set ORI_RESEARCH_PYTHON to the installed isolated research interpreter"),
        args: vec!["-I".into(), "-B".into(), "-m".into(), "ori_research".into()],
        environment: BTreeMap::new(),
        deadline_ms: 10_000,
        require_hard_memory: false,
        linux_cgroup: None,
    }
}
async fn service(archive: Archive, root: &std::path::Path) -> HistoryService {
    HistoryService::new(
        archive,
        ResearchPool::default(),
        spec(),
        HistoryConfig::new(root.join("cache"), root.join("snapshots")),
    )
    .await
    .unwrap()
}

#[tokio::test]
async fn viewport_preserves_first_peak_ties_bits_exact_ids_and_frozen_page_boundary() {
    let directory = tempfile::tempdir_in(std::env::temp_dir().canonicalize().unwrap()).unwrap();
    let archive =
        Archive::open(ArchiveConfig::new(directory.path().join("source.sqlite"))).unwrap();
    archive.create_session(session()).await.unwrap();
    for (id, micros) in [("é", 1), ("e\u{301}", 2)] {
        let mut sweep = fixture()["sweep"].clone();
        sweep["id"] = json!(id);
        sweep["interval"] = json!({"started_at":format!("2026-11-11T12:00:00.{micros:06}Z"),"ended_at":"2026-11-11T12:00:01Z"});
        sweep["frequencies_hz"] = json!([100, 101, 102, 103]);
        sweep["levels"] = json!([-0.0, 0.0, -1.0000000000000002, -50.0]);
        archive.append_sweeps(vec![sweep]).await.unwrap();
    }
    let history = service(archive.clone(), directory.path()).await;
    let mut query = ViewportQuery::new("fixture-сессия", "spectrum/one", 100, 103, 2, 1);
    let first = history.viewport(query.clone()).await.unwrap();
    assert_eq!(first.boundary, 2);
    assert_eq!(first.rows.len(), 1);
    assert_eq!(
        first.rows[0].measurement_id.as_bytes(),
        "e\u{301}".as_bytes()
    );
    assert_eq!(first.rows[0].cells[0].point_index, 0);
    assert_eq!(first.rows[0].cells[0].level.to_bits(), (-0.0_f64).to_bits());
    assert_eq!(first.rows[0].cells[1].point_index, 2);
    assert_eq!(
        first.rows[0].cells[1].level.to_bits(),
        (-1.0000000000000002_f64).to_bits()
    );
    let cursor = first.next_cursor.clone().unwrap();
    let mut later = fixture()["sweep"].clone();
    later["id"] = json!("late-arrival");
    archive.append_sweeps(vec![later]).await.unwrap();
    query.cursor = Some(cursor.clone());
    let second = history.viewport(query.clone()).await.unwrap();
    assert_eq!(second.boundary, 2);
    assert_eq!(second.rows[0].measurement_id.as_bytes(), "é".as_bytes());
    assert!(second.next_cursor.is_none());
    assert_ne!(first.cache_key, second.cache_key);
    query.columns = 1;
    assert!(
        history.viewport(query).await.is_err(),
        "a cursor must bind the original viewport query"
    );
    assert!(archive.get_sweep("late-arrival").await.unwrap().is_some());
    history.close().await.unwrap();
    archive.close().await.unwrap();
}

#[tokio::test]
async fn heterogeneous_rows_are_marked_and_never_reduced_against_each_other() {
    let directory = tempfile::tempdir_in(std::env::temp_dir().canonicalize().unwrap()).unwrap();
    let archive =
        Archive::open(ArchiveConfig::new(directory.path().join("source.sqlite"))).unwrap();
    archive.create_session(session()).await.unwrap();
    for index in 0..3 {
        let mut sweep = fixture()["sweep"].clone();
        sweep["id"] = json!(format!("w-{index}"));
        sweep["settings"] = if index == 0 {
            json!({})
        } else {
            json!({"rbw_hz":index})
        };
        archive.append_sweeps(vec![sweep]).await.unwrap();
    }
    let history = service(archive.clone(), directory.path()).await;
    let query = ViewportQuery::new("fixture-сессия", "spectrum/one", 100000001, 100000004, 2, 4);
    let page = history.viewport(query).await.unwrap();
    assert_eq!(page.rows.len(), 3);
    assert!(page.quality.incomplete_settings);
    assert!(page.quality.heterogeneous);
    assert!(page.rows.iter().all(|row| row.cells.len() <= 2));
    assert!(
        page.rows
            .windows(2)
            .all(|rows| rows[0].measurement_id.as_bytes() < rows[1].measurement_id.as_bytes())
    );
    history.close().await.unwrap();
    archive.close().await.unwrap();
}

#[tokio::test]
async fn invalid_source_ranges_or_large_render_requests_fail_before_reserving_work() {
    let directory = tempfile::tempdir_in(std::env::temp_dir().canonicalize().unwrap()).unwrap();
    let archive =
        Archive::open(ArchiveConfig::new(directory.path().join("source.sqlite"))).unwrap();
    let history = service(archive.clone(), directory.path()).await;
    for query in [
        ViewportQuery::new("s", "a", 200, 100, 10, 10),
        ViewportQuery::new("s", "a", 0, i64::MAX, 1025, 1),
        ViewportQuery::new("s", "a", 0, 1000, 1024, 100),
    ] {
        assert!(history.viewport(query).await.is_err());
    }
    history.close().await.unwrap();
    archive.close().await.unwrap();
}

fn source(id: &str, micros: u32, unit: &str) -> Value {
    let mut value = fixture()["sweep"].clone();
    value["id"] = json!(id);
    value["interval"] = json!({"started_at":format!("2026-11-11T12:00:00.{micros:06}Z"),
        "ended_at":"2026-11-11T12:00:01.000001Z"});
    value["frequencies_hz"] = json!([100, 101, 102, 103]);
    value["levels"] = json!([-0.0, 0.0, -1.0000000000000002, -50.0]);
    value["level_unit"] = json!(unit);
    value
}

#[tokio::test]
async fn five_original_rows_replay_issued_cursors_without_id_restrictions_or_new_arrivals() {
    let directory = tempfile::tempdir_in(std::env::temp_dir().canonicalize().unwrap()).unwrap();
    let archive =
        Archive::open(ArchiveConfig::new(directory.path().join("source.sqlite"))).unwrap();
    let unit = "uncalibrated raw e\u{301}\nunit ".repeat(12);
    let mut value = session();
    value["streams"]
        .as_array_mut()
        .unwrap()
        .iter_mut()
        .find(|stream| stream["id"] == "spectrum/one")
        .unwrap()["level_unit"] = json!(unit);
    archive.create_session(value).await.unwrap();
    let long_id = format!("z{}", "x".repeat(1024));
    let ids = [
        "control\tID\0end".to_owned(),
        " space ID /?%+ ".to_owned(),
        long_id,
        "é".into(),
        "e\u{301}".into(),
    ];
    let originals = ids
        .iter()
        .zip([6, 5, 4, 3, 3])
        .map(|(id, micros)| source(id, micros, &unit))
        .collect::<Vec<_>>();
    archive.append_sweeps(originals.clone()).await.unwrap();
    let history = service(archive.clone(), directory.path()).await;
    let mut query = ViewportQuery::new("fixture-сессия", "spectrum/one", 100, 103, 2, 1);
    let mut found = Vec::new();
    for index in 0..5 {
        let page = history
            .viewport(query.clone())
            .await
            .expect("an issued cursor must replay the original five source rows");
        assert_eq!(page.boundary, 5);
        assert_eq!(page.rows.len(), 1);
        let row = &page.rows[0];
        assert_eq!(row.measurement_id.as_bytes(), ids[index].as_bytes());
        assert_eq!(row.level_unit.as_bytes(), unit.as_bytes());
        let base = "2026-11-11T12:00:00Z"
            .parse::<ori_contracts::UtcTimestamp>()
            .unwrap()
            .unix_micros();
        assert_eq!(row.started_at_us, base + [6, 5, 4, 3, 3][index]);
        assert_eq!(row.ended_at_us, base + 1_000_001);
        assert_eq!(row.cells[0].level.to_bits(), (-0.0_f64).to_bits());
        assert_eq!(
            row.cells[1].level.to_bits(),
            (-1.0000000000000002_f64).to_bits()
        );
        assert_eq!(
            (row.cells[0].frequency_hz, row.cells[1].point_index),
            (100, 2)
        );
        found.push(row.measurement_id.clone());
        if index == 0 {
            archive
                .append_sweeps(vec![source("new arrival after frozen boundary", 7, &unit)])
                .await
                .unwrap();
        }
        if let Some(cursor) = page.next_cursor {
            let bytes = ori_contracts::to_json_vec(&cursor).unwrap();
            assert!(bytes.len() <= 4096);
            let value = ori_contracts::parse_json_value(&bytes, 4096).unwrap();
            query.cursor = Some(serde_json::from_value::<ViewportCursor>(value).unwrap());
            // Replay the same immutable query through the actual cache path.
            if index == 1 {
                let cached = history.viewport(query.clone()).await.unwrap();
                assert_eq!(cached.boundary, 5);
                assert_eq!(
                    cached.rows[0].measurement_id.as_bytes(),
                    ids[index + 1].as_bytes()
                );
            }
        } else {
            assert_eq!(index, 4);
        }
    }
    assert_eq!(found, ids);
    for original in originals {
        let id = original["id"].as_str().unwrap();
        assert_eq!(archive.get_sweep(id).await.unwrap().unwrap(), original);
    }
    assert!(
        archive
            .get_sweep("new arrival after frozen boundary")
            .await
            .unwrap()
            .is_some()
    );
    history.close().await.unwrap();
    archive.close().await.unwrap();
}

#[tokio::test]
async fn untransportable_escape_or_nfd_cursor_fails_before_issuing_or_caching_a_page() {
    for id in [
        format!("original{}", "\u{1}".repeat(700)),
        "e\u{301}".repeat(1400),
    ] {
        let directory = tempfile::tempdir_in(std::env::temp_dir().canonicalize().unwrap()).unwrap();
        let archive =
            Archive::open(ArchiveConfig::new(directory.path().join("source.sqlite"))).unwrap();
        archive.create_session(session()).await.unwrap();
        let originals = vec![source(&id, 2, "dBm"), source("older", 1, "dBm")];
        archive.append_sweeps(originals.clone()).await.unwrap();
        let before = archive.events(0, 100).await.unwrap();
        let history = service(archive.clone(), directory.path()).await;
        let query = ViewportQuery::new("fixture-сессия", "spectrum/one", 100, 103, 2, 1);
        assert!(
            matches!(
                history.viewport(query).await,
                Err(HistoryError::ResourceLimit)
            ),
            "must not return a cursor the 4096-byte JSON transport cannot accept"
        );
        assert_eq!(
            std::fs::read_dir(directory.path().join("cache/views"))
                .unwrap()
                .count(),
            0
        );
        assert_eq!(archive.events(0, 100).await.unwrap(), before);
        for original in originals {
            assert_eq!(
                archive
                    .get_sweep(original["id"].as_str().unwrap())
                    .await
                    .unwrap()
                    .unwrap(),
                original
            );
        }
        history.close().await.unwrap();
        archive.close().await.unwrap();
    }
}

#[tokio::test]
async fn an_old_cached_page_with_an_untransportable_cursor_is_not_reissued() {
    let directory = tempfile::tempdir_in(std::env::temp_dir().canonicalize().unwrap()).unwrap();
    let archive =
        Archive::open(ArchiveConfig::new(directory.path().join("source.sqlite"))).unwrap();
    archive.create_session(session()).await.unwrap();
    archive
        .append_sweeps(vec![source("newer", 2, "dBm"), source("older", 1, "dBm")])
        .await
        .unwrap();
    let history = service(archive.clone(), directory.path()).await;
    let query = ViewportQuery::new("fixture-сессия", "spectrum/one", 100, 103, 2, 1);
    let mut page = history.viewport(query.clone()).await.unwrap();
    page.next_cursor.as_mut().unwrap().after.id = "e\u{301}".repeat(1400);
    let path = directory
        .path()
        .join(format!("cache/views/vp-{}.json", page.cache_key));
    std::fs::write(path, ori_contracts::to_json_vec(&page).unwrap()).unwrap();
    assert!(
        matches!(
            history.viewport(query).await,
            Err(HistoryError::ResourceLimit)
        ),
        "cached output must obey the same replayable cursor budget"
    );
    assert!(archive.get_sweep("newer").await.unwrap().is_some());
    history.close().await.unwrap();
    archive.close().await.unwrap();
}

#[test]
fn full_serialized_cursor_budget_accepts_exact_boundary_with_ascii_nfd_and_escapes() {
    for raw_prefix in [
        "x".repeat(1500),
        "e\u{301}".repeat(500),
        "\u{1}".repeat(200),
    ] {
        let mut query = ViewportQuery::new("session", "stream", 100, 103, 2, 1);
        let mut cursor = ViewportCursor {
            boundary: 1,
            query_key: "a".repeat(64),
            after: ori_archive::MeasurementCursor {
                started_at: "2026-11-11T12:00:00.000001Z".into(),
                id: raw_prefix,
            },
        };
        let used = ori_contracts::to_json_vec(&cursor).unwrap().len();
        cursor.after.id.push_str(&"x".repeat(4096 - used));
        assert_eq!(ori_contracts::to_json_vec(&cursor).unwrap().len(), 4096);
        query.cursor = Some(cursor);
        assert!(
            query.validate().is_ok(),
            "an otherwise valid exact 4096-byte cursor is admissible"
        );
        query.cursor.as_mut().unwrap().after.id.push('x');
        assert_eq!(
            ori_contracts::to_json_vec(query.cursor.as_ref().unwrap())
                .unwrap()
                .len(),
            4097
        );
        assert!(matches!(query.validate(), Err(HistoryError::Invalid(_))));
    }
}

#[tokio::test]
async fn oversized_cursor_is_rejected_before_the_held_heavy_admission_is_consulted() {
    let directory = tempfile::tempdir_in(std::env::temp_dir().canonicalize().unwrap()).unwrap();
    let archive =
        Archive::open(ArchiveConfig::new(directory.path().join("source.sqlite"))).unwrap();
    archive.create_session(session()).await.unwrap();
    let pool = ResearchPool::default();
    let history = HistoryService::new(
        archive.clone(),
        pool.clone(),
        spec(),
        HistoryConfig::new(
            directory.path().join("cache"),
            directory.path().join("snapshots"),
        ),
    )
    .await
    .unwrap();
    let held = pool.try_reserve().unwrap();
    let mut query = ViewportQuery::new("fixture-сессия", "spectrum/one", 100, 103, 2, 1);
    query.cursor = Some(ViewportCursor {
        boundary: 0,
        query_key: "a".repeat(64),
        after: ori_archive::MeasurementCursor {
            started_at: "2026-11-11T12:00:00.000001Z".into(),
            id: "small original ID".into(),
        },
    });
    assert!(matches!(
        history.viewport(query.clone()).await,
        Err(HistoryError::Busy)
    ));
    query.cursor.as_mut().unwrap().after.id = "x".repeat(4096);
    assert!(matches!(
        history.viewport(query).await,
        Err(HistoryError::Invalid(_))
    ));
    assert!(pool.try_reserve().is_err());
    drop(held);
    history.close().await.unwrap();
    archive.close().await.unwrap();
}
