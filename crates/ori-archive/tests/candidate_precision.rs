use ori_archive::{Archive, ArchiveConfig, EventDraft, Mutation, restore_backup};
use serde_json::{Value, json};

fn session() -> Value {
    let mut value: Value = serde_json::from_str(include_str!(
        "../../../contracts/fixtures/measurement-v1.json"
    ))
    .unwrap();
    value["session"]["created_at"] = json!("2026-11-11T00:00:00.123456Z");
    value["session"].clone()
}
fn candidate() -> Value {
    json!({"id":"candidate-𐀀","session_id":"fixture-сессия","source_stream_id":"spectrum/one",
      "interval":{"started_at":"2026-11-11T00:00:00.123456Z","ended_at":"2026-11-11T00:00:00.223456Z"},
      "center_frequency_hz":100000001,"peak_level":-0.0,"score":1.0000000000000002,
      "level_unit":"dBm","state":"new","observation_ids":[]})
}
async fn save(archive: &Archive, value: Value) -> ori_archive::Result<ori_archive::CommittedBatch> {
    archive
        .apply(vec![Mutation::Candidate {
            event: Some(EventDraft::now(
                "candidate.created",
                Some("fixture-сессия".into()),
                json!({"candidate":value}),
            )),
            value,
        }])
        .await
}
fn exact(value: &Value) {
    assert_eq!(
        value["peak_level"].as_f64().unwrap().to_bits(),
        (-0.0_f64).to_bits()
    );
    assert_eq!(
        value["score"].as_f64().unwrap().to_bits(),
        1.0000000000000002_f64.to_bits()
    );
}

#[tokio::test]
async fn stopping_an_open_investigation_preserves_candidate_bits_in_rows_and_events() {
    let temp = tempfile::tempdir().unwrap();
    let archive = Archive::open(ArchiveConfig::new(temp.path().join("archive.sqlite"))).unwrap();
    let mut running = session();
    running["state"] = json!("running");
    archive.create_session(running.clone()).await.unwrap();
    archive
        .apply(vec![Mutation::Investigation {
            session_id: "fixture-сессия".into(),
            value: json!({"id":"open-job","candidate_id":"candidate-𐀀",
                "target_stream_id":"receiver+one","target_frequency_hz":100000001,
                "state":"listening","created_at":"2026-11-11T00:00:00.223456Z"}),
            candidate: candidate(),
            event: None,
        }])
        .await
        .unwrap();
    let saved = archive
        .stop_session(running, "2026-11-11T00:01:00Z")
        .await
        .unwrap();
    assert_eq!(saved.events.len(), 2);
    assert_eq!(saved.events[1]["event_type"], "investigation.cancelled");
    exact(&saved.events[1]["payload"]["candidate"]);
    let candidate = archive.get_candidate("candidate-𐀀").await.unwrap().unwrap();
    assert_eq!(candidate["state"], "queued");
    exact(&candidate);
    assert_eq!(
        archive
            .get_investigation("open-job")
            .await
            .unwrap()
            .unwrap()
            .1["state"],
        "cancelled"
    );
    archive.close().await.unwrap();
}

#[tokio::test]
async fn new_scalar_bits_survive_get_list_snapshot_reopen_and_private_restore() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("archive.sqlite");
    let archive = Archive::open(ArchiveConfig::new(&path)).unwrap();
    archive.create_session(session()).await.unwrap();
    save(&archive, candidate()).await.unwrap();
    exact(&archive.get_candidate("candidate-𐀀").await.unwrap().unwrap());
    exact(
        &archive
            .list_candidates("fixture-сессия", None, 10, None)
            .await
            .unwrap()
            .items[0],
    );
    exact(
        &archive
            .analysis_inputs("fixture-сессия", vec![], vec!["candidate-𐀀".into()])
            .await
            .unwrap()
            .2[0],
    );
    let backup = temp.path().join("backup");
    archive.backup(&backup).await.unwrap();
    archive.close().await.unwrap();
    let restored = temp.path().join("restored.sqlite");
    restore_backup(&backup, &restored).unwrap();
    for file in [&path, &restored] {
        let reopened = Archive::open(ArchiveConfig::new(file)).unwrap();
        exact(
            &reopened
                .get_candidate("candidate-𐀀")
                .await
                .unwrap()
                .unwrap(),
        );
        reopened.close().await.unwrap();
    }
}

#[tokio::test]
async fn missing_historical_bytes_are_not_guessed_and_null_update_clears_supplement() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("archive.sqlite");
    let archive = Archive::open(ArchiveConfig::new(&path)).unwrap();
    archive.create_session(session()).await.unwrap();
    save(&archive, candidate()).await.unwrap();
    let db = rusqlite::Connection::open(&path).unwrap();
    db.execute("DELETE FROM ori_candidate_scalar_bits", [])
        .unwrap();
    let legacy = archive.get_candidate("candidate-𐀀").await.unwrap().unwrap();
    assert_eq!(
        legacy["peak_level"].as_f64().unwrap().to_bits(),
        0.0_f64.to_bits()
    );
    let mut value = candidate();
    value["peak_level"] = Value::Null;
    value["score"] = Value::Null;
    save(&archive, value).await.unwrap();
    assert_eq!(
        db.query_row("SELECT COUNT(*) FROM ori_candidate_scalar_bits", [], |r| {
            r.get::<_, i64>(0)
        })
        .unwrap(),
        0
    );
    archive.close().await.unwrap();
}

#[tokio::test]
async fn invalid_event_rolls_back_bits_and_corrupt_bytes_fail_read_and_backup() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("archive.sqlite");
    let archive = Archive::open(ArchiveConfig::new(&path)).unwrap();
    archive.create_session(session()).await.unwrap();
    let db = rusqlite::Connection::open(&path).unwrap();
    db.execute_batch("CREATE TRIGGER reject_candidate BEFORE INSERT ON events WHEN NEW.event_type='candidate.created' BEGIN SELECT RAISE(ABORT,'synthetic failure'); END;").unwrap();
    assert!(save(&archive, candidate()).await.is_err());
    assert_eq!(
        db.query_row("SELECT COUNT(*) FROM ori_candidate_scalar_bits", [], |r| {
            r.get::<_, i64>(0)
        })
        .unwrap(),
        0
    );
    assert!(
        archive
            .get_candidate("candidate-𐀀")
            .await
            .unwrap()
            .is_none()
    );
    db.execute_batch("DROP TRIGGER reject_candidate;").unwrap();
    save(&archive, candidate()).await.unwrap();
    db.execute(
        "UPDATE ori_candidate_scalar_bits SET peak_level_bits_le=?",
        [5.0_f64.to_le_bytes().to_vec()],
    )
    .unwrap();
    assert!(archive.get_candidate("candidate-𐀀").await.is_err());
    assert!(
        archive
            .backup(&temp.path().join("corrupt-copy"))
            .await
            .is_err()
    );
    archive.close().await.unwrap();
}

#[tokio::test]
async fn future_or_partial_extension_is_refused_without_rewriting_the_archive() {
    for partial in [false, true] {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("archive.sqlite");
        let archive = Archive::open(ArchiveConfig::new(&path)).unwrap();
        archive.close().await.unwrap();
        {
            let db = rusqlite::Connection::open(&path).unwrap();
            db.execute_batch("CREATE TABLE IF NOT EXISTS ori_candidate_precision_schema(singleton INTEGER PRIMARY KEY,version INTEGER NOT NULL);INSERT OR REPLACE INTO ori_candidate_precision_schema VALUES(1,2);").unwrap();
            if partial {
                db.execute_batch("DROP TABLE IF EXISTS ori_candidate_scalar_bits;")
                    .unwrap();
            } else {
                db.execute_batch("CREATE TABLE IF NOT EXISTS ori_candidate_scalar_bits(candidate_id TEXT PRIMARY KEY,peak_level_bits_le BLOB,score_bits_le BLOB);").unwrap();
            }
            db.pragma_update(None, "journal_mode", "DELETE").unwrap();
        }
        let before = std::fs::read(&path).unwrap();
        assert!(Archive::open(ArchiveConfig::new(&path)).is_err());
        assert_eq!(std::fs::read(&path).unwrap(), before);
    }
}
