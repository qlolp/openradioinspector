use ori_archive::{Archive, ArchiveConfig};
use serde_json::{Value, json};

fn fixture() -> Value {
    serde_json::from_str(include_str!(
        "../../../contracts/fixtures/measurement-v1.json"
    ))
    .unwrap()
}
fn session() -> Value {
    let mut session = fixture()["session"].clone();
    session["created_at"] = json!("2026-11-11T12:00:00.123456Z");
    session
}

#[tokio::test]
async fn committed_sources_reopen_with_original_ids_utc_microseconds_and_f64_bits() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("archive.sqlite");
    let archive = Archive::open(ArchiveConfig::new(&path)).unwrap();
    let first = archive.create_session(session()).await.unwrap();
    assert_eq!(first.events[0]["sequence"], 1);
    let committed = archive
        .append_sweeps(vec![fixture()["sweep"].clone()])
        .await
        .unwrap();
    assert_eq!(committed.events[0]["sequence"], 2);
    assert_eq!(committed.ingestion_boundary, Some(1));
    let mut observation = fixture()["observation"].clone();
    observation["level"] = json!(-0.0);
    archive
        .append_observations(vec![observation])
        .await
        .unwrap();
    archive.close().await.unwrap();
    let archive = Archive::open(ArchiveConfig::new(&path)).unwrap();
    let sweep = archive.get_sweep("fixture-sweep-α").await.unwrap().unwrap();
    assert_eq!(
        sweep["interval"]["started_at"],
        "2026-11-11T12:00:00.123456Z"
    );
    for (number, bits) in sweep["levels"].as_array().unwrap().iter().zip([
        0xc059080000000000_u64,
        0x8000000000000000,
        0xbff0000000000001,
        0xc040600000000000,
    ]) {
        assert_eq!(number.as_f64().unwrap().to_bits(), bits);
    }
    let observation = archive
        .get_observation("fixture-observation-α")
        .await
        .unwrap()
        .unwrap();
    assert_eq!(
        observation["level"].as_f64().unwrap().to_bits(),
        (-0.0_f64).to_bits()
    );
    assert_eq!(observation["level_unit"], "s_meter_count");
    assert_eq!(archive.events(0, 10).await.unwrap().len(), 3);
    let status = archive.status().await.unwrap();
    assert_eq!(status["journal_mode"], "wal");
    assert_eq!(status["synchronous"], 2);
    assert_eq!(status["schema_version"], 6);
    assert_eq!(status["monitoring_schema_version"], 4);
    assert_eq!(status["precision_schema_version"], 1);
    archive.close().await.unwrap();
}

#[tokio::test]
async fn duplicate_or_foreign_batch_rolls_back_sources_events_and_ingestion_cursor() {
    let directory = tempfile::tempdir().unwrap();
    let archive =
        Archive::open(ArchiveConfig::new(directory.path().join("archive.sqlite"))).unwrap();
    archive.create_session(session()).await.unwrap();
    let mut first = fixture()["sweep"].clone();
    first["id"] = json!("atomic-a");
    let mut foreign = first.clone();
    foreign["id"] = json!("atomic-b");
    foreign["stream_id"] = json!("foreign-stream");
    assert!(
        archive
            .append_sweeps(vec![first.clone(), foreign])
            .await
            .is_err()
    );
    assert!(archive.get_sweep("atomic-a").await.unwrap().is_none());
    assert_eq!(archive.events(0, 10).await.unwrap().len(), 1);
    assert!(
        archive
            .append_sweeps(vec![first.clone(), first.clone()])
            .await
            .is_err()
    );
    let committed = archive.append_sweeps(vec![first]).await.unwrap();
    assert_eq!(committed.ingestion_boundary, Some(1));
    assert_eq!(committed.events[0]["sequence"], 2);
    archive.close().await.unwrap();
}

#[tokio::test]
async fn python_utf16_v5_migration_preserves_every_original_raw_and_metadata_row() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("legacy.sqlite");
    std::fs::copy(
        concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/tests/fixtures/legacy_v5_utf16.sqlite"
        ),
        &path,
    )
    .unwrap();
    let before = std::fs::read(&path).unwrap();
    let archive = Archive::open(ArchiveConfig::new(&path)).unwrap();
    assert!(archive.get_sweep("tie-\u{10000}").await.unwrap().is_some());
    archive.close().await.unwrap();
    let db = rusqlite::Connection::open(&path).unwrap();
    assert!(
        db.query_row("PRAGMA encoding", [], |r| r.get::<_, String>(0))
            .unwrap()
            .starts_with("UTF-16")
    );
    let expected: Value =
        serde_json::from_str(include_str!("fixtures/legacy_v5_utf16.snapshot.json")).unwrap();
    for (table, rows) in expected.as_object().unwrap() {
        let mut stmt = db
            .prepare(&format!("SELECT * FROM {table} ORDER BY rowid"))
            .unwrap();
        let columns = stmt.column_count();
        let actual:Vec<Value>=stmt.query_map([],|row| { let values=(0..columns).map(|i| match row.get_ref(i).unwrap(){rusqlite::types::ValueRef::Null=>Value::Null,rusqlite::types::ValueRef::Integer(x)=>json!(x),rusqlite::types::ValueRef::Real(x)=>json!(x),rusqlite::types::ValueRef::Text(x)=>json!(std::str::from_utf8(x).unwrap()),rusqlite::types::ValueRef::Blob(x)=>json!({"bytes_hex":x.iter().map(|byte|format!("{byte:02x}")).collect::<String>()})}).collect::<Vec<_>>();Ok(json!(values)) }).unwrap().map(|r|r.unwrap()).collect();
        assert_eq!(json!(actual), *rows, "historical table {table} changed");
    }
    let ingestion: Vec<String> = db
        .prepare("SELECT sweep_id FROM sweep_ingestion ORDER BY sequence")
        .unwrap()
        .query_map([], |r| r.get(0))
        .unwrap()
        .map(|r| r.unwrap())
        .collect();
    let expected_order: Vec<String> = db
        .prepare("SELECT id FROM sweeps ORDER BY id")
        .unwrap()
        .query_map([], |r| r.get(0))
        .unwrap()
        .map(|r| r.unwrap())
        .collect();
    assert_eq!(ingestion, expected_order);
    assert_ne!(
        std::fs::read(path).unwrap(),
        before,
        "migration must actually add the ingestion table"
    );
}

#[test]
fn future_versions_are_rejected_without_changing_a_single_database_byte() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("future.sqlite");
    std::fs::copy(
        concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/tests/fixtures/legacy_v5_utf16.sqlite"
        ),
        &path,
    )
    .unwrap();
    {
        let db = rusqlite::Connection::open(&path).unwrap();
        db.execute_batch("PRAGMA user_version=99").unwrap();
    }
    let before = std::fs::read(&path).unwrap();
    assert!(Archive::open(ArchiveConfig::new(&path)).is_err());
    assert_eq!(before, std::fs::read(path).unwrap());
}

#[tokio::test]
async fn metadata_private_serde_keys_and_arbitrary_integers_remain_json_objects_and_exact_numbers()
{
    let directory = tempfile::tempdir().unwrap();
    let archive =
        Archive::open(ArchiveConfig::new(directory.path().join("archive.sqlite"))).unwrap();
    let mut source = session();
    source["metadata"] = json!({"alias":{"$serde_json::private::Number":"123"},"raw_alias":{"$serde_json::private::RawValue":"{\"not\":\"an injected object\"}"}});
    source["metadata"]["huge"] =
        Value::Number("1234567890123456789012345678901234567890".parse().unwrap());
    let expected = source["metadata"].clone();
    archive.create_session(source).await.unwrap();
    let actual = archive
        .get_session("fixture-сессия")
        .await
        .unwrap()
        .unwrap();
    assert_eq!(actual["metadata"], expected);
    assert_eq!(
        actual["metadata"]["huge"].to_string(),
        "1234567890123456789012345678901234567890"
    );
    archive.close().await.unwrap();
}

#[test]
fn incomplete_or_future_precision_and_monitoring_schemas_fail_before_any_database_change() {
    for ddl in [
        "CREATE TABLE ori_archive_schema(singleton INTEGER PRIMARY KEY,version INTEGER);INSERT INTO ori_archive_schema VALUES(1,1)",
        "CREATE TABLE ori_observation_level_bits(observation_id TEXT PRIMARY KEY,level_bits_le BLOB)",
        "CREATE TABLE ori_archive_schema(singleton INTEGER PRIMARY KEY,version INTEGER);INSERT INTO ori_archive_schema VALUES(1,0);CREATE TABLE ori_observation_level_bits(observation_id TEXT PRIMARY KEY,level_bits_le BLOB)",
        "CREATE TABLE ori_archive_schema(singleton INTEGER PRIMARY KEY,version INTEGER);INSERT INTO ori_archive_schema VALUES(1,2);CREATE TABLE ori_observation_level_bits(observation_id TEXT PRIMARY KEY,level_bits_le BLOB)",
        "UPDATE monitoring_schema SET version=5",
        "CREATE TABLE ori_archive_query_schema(singleton INTEGER PRIMARY KEY,version INTEGER);INSERT INTO ori_archive_query_schema VALUES(1,2)",
    ] {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("future.sqlite");
        std::fs::copy(
            concat!(
                env!("CARGO_MANIFEST_DIR"),
                "/tests/fixtures/legacy_v5_utf16.sqlite"
            ),
            &path,
        )
        .unwrap();
        {
            let db = rusqlite::Connection::open(&path).unwrap();
            db.execute_batch(ddl).unwrap();
        }
        let before = std::fs::read(&path).unwrap();
        assert!(
            Archive::open(ArchiveConfig::new(&path)).is_err(),
            "accepted {ddl}"
        );
        assert_eq!(before, std::fs::read(path).unwrap());
    }
}

#[tokio::test]
async fn settings_metadata_and_event_payload_preserve_reserved_key_objects_and_large_integers_after_reopen()
 {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("archive.sqlite");
    let unusual=ori_archive::parse_json_value(br#"{"$serde_json::private::Number":{"$serde_json::private::RawValue":"123"},"__ori_reserved_0":"literal","huge":184467440737095516160000,"negative_zero":-0.0}"#,4096).unwrap();
    let archive = Archive::open(ArchiveConfig::new(&path)).unwrap();
    let mut value = session();
    value["metadata"] = unusual.clone();
    value["streams"][0]["settings"] = unusual.clone();
    archive.create_session(value).await.unwrap();
    let mut sweep = fixture()["sweep"].clone();
    sweep["settings"] = unusual.clone();
    archive.append_sweeps(vec![sweep]).await.unwrap();
    let mut obs = fixture()["observation"].clone();
    obs["metadata"] = unusual.clone();
    archive.append_observations(vec![obs]).await.unwrap();
    archive.close().await.unwrap();
    let archive = Archive::open(ArchiveConfig::new(path)).unwrap();
    let value = archive
        .get_session("fixture-сессия")
        .await
        .unwrap()
        .unwrap();
    assert_eq!(value["metadata"], unusual);
    assert_eq!(value["streams"][0]["settings"], unusual);
    assert_eq!(
        archive.get_sweep("fixture-sweep-α").await.unwrap().unwrap()["settings"],
        unusual
    );
    assert_eq!(
        archive
            .get_observation("fixture-observation-α")
            .await
            .unwrap()
            .unwrap()["metadata"],
        unusual
    );
    assert_eq!(
        archive.events(1, 10).await.unwrap()[0]["payload"]["sweep"]["settings"],
        unusual
    );
    archive.close().await.unwrap();
}

#[test]
fn parser_rejects_duplicate_keys_before_mutation() {
    assert!(ori_archive::parse_json_value(br#"{"same":1,"same":2}"#, 1024).is_err());
}
