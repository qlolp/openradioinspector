use ori_archive::{
    Archive, ArchiveConfig, EventDraft, Mutation, init_compact_archive, materialize_archive,
    materialize_stopped_archive, restore_backup, verify_backup,
};
use rusqlite::Connection;
use serde_json::{Value, json};
use std::{fs, path::Path, process::Command};

fn fixture() -> Value {
    ori_archive::parse_json_value(
        include_bytes!("../../../contracts/fixtures/measurement-v1.json"),
        65536,
    )
    .unwrap()
}

fn inspect(path: &Path, version: i64) {
    let db = Connection::open(path).unwrap();
    assert_eq!(
        db.query_row("PRAGMA user_version", [], |row| row.get::<_, i64>(0))
            .unwrap(),
        version
    );
    let raw: String = db.query_row("SELECT payload_json FROM events WHERE event_type='sweep.received' ORDER BY sequence LIMIT 1",
                                  [], |row| row.get(0)).unwrap();
    assert_eq!(raw.contains("\"frequencies_hz\""), version == 6);
    assert_eq!(raw.contains("\"levels\""), version == 6);
}

fn python(script: &str, paths: &[&Path]) -> Vec<u8> {
    let output =
        Command::new(std::env::var_os("ORI_TEST_PYTHON").unwrap_or_else(|| "python".into()))
            .args(["-c", script])
            .args(paths)
            .env("PYTHONDONTWRITEBYTECODE", "1")
            .env("OPENBLAS_NUM_THREADS", "1")
            .env("OMP_NUM_THREADS", "1")
            .current_dir(concat!(env!("CARGO_MANIFEST_DIR"), "/../.."))
            .output()
            .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    output.stdout
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn stopped_export_preserves_committed_wal_and_refuses_a_pinned_reader() {
    let temp = tempfile::tempdir().unwrap();
    let source = temp.path().join("source.sqlite");
    let converted = temp.path().join("converted.sqlite");
    init_compact_archive(&source).await.unwrap();
    let archive = Archive::open(ArchiveConfig::new(&source)).unwrap();
    let mut session = fixture()["session"].clone();
    session["created_at"] = json!("2026-11-11T00:00:00Z");
    archive.create_session(session).await.unwrap();
    archive
        .append_sweeps(vec![fixture()["sweep"].clone()])
        .await
        .unwrap();
    let events = archive.events(0, 20).await.unwrap();
    archive.close().await.unwrap();

    // Put a committed page solely in the WAL even if background maintenance just ran.
    let writer = Connection::open(&source).unwrap();
    writer
        .set_db_config(
            rusqlite::config::DbConfig::SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE,
            true,
        )
        .unwrap();
    writer
        .execute_batch("PRAGMA wal_autocheckpoint=0; PRAGMA application_id=424242")
        .unwrap();
    writer.close().unwrap();
    let wal = temp.path().join("source.sqlite-wal");
    assert!(fs::metadata(&wal).unwrap().len() > 32);
    let pinned =
        Connection::open_with_flags(&source, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY).unwrap();
    pinned
        .execute_batch("BEGIN; SELECT COUNT(*) FROM events")
        .unwrap();
    assert!(materialize_stopped_archive(&source, &converted).is_err());
    assert!(!converted.exists());
    assert!(fs::metadata(&wal).unwrap().len() > 32);
    pinned.close().unwrap();

    materialize_stopped_archive(&source, &converted).unwrap();
    for (name, file, version) in [("source", &source, 7), ("converted", &converted, 6)] {
        for suffix in ["-wal", "-shm", "-journal"] {
            assert!(!temp.path().join(format!("{name}.sqlite{suffix}")).exists());
        }
        // Open the main file alone in a new directory, just as the old package would.
        let directory = tempfile::tempdir().unwrap();
        let standalone = directory.path().join("sessions.sqlite3");
        fs::copy(file, &standalone).unwrap();
        inspect(&standalone, version);
        let db = Connection::open(&standalone).unwrap();
        assert_eq!(
            db.query_row("PRAGMA application_id", [], |row| row.get::<_, i64>(0))
                .unwrap(),
            424242
        );
        db.close().unwrap();
        let checked = Archive::open(ArchiveConfig::new(&standalone)).unwrap();
        assert_eq!(checked.events(0, 20).await.unwrap(), events);
        checked.close().await.unwrap();
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn captured_replayed_restarted_backed_up_and_materialized_events_match_under_both_readers() {
    let temp = tempfile::tempdir().unwrap();
    let live = temp.path().join("live.sqlite");
    init_compact_archive(&live).await.unwrap();
    let archive = Archive::open(ArchiveConfig::new(&live)).unwrap();
    let mut session = fixture()["session"].clone();
    session["created_at"] = json!("2026-11-11T00:00:00Z");
    archive.create_session(session).await.unwrap();
    let mut sweep = fixture()["sweep"].clone();
    sweep["interval"]["ticks"] = json!(9_007_199_254_740_993_i64);
    sweep["vendor"] = json!({"$serde_json::private::Number":"literal"});
    let emitted = archive.append_sweeps(vec![sweep]).await.unwrap().events;
    let note = EventDraft::now("operator.note", None, json!({"ori_sweep_ref_v1":"literal"}));
    archive.apply(vec![Mutation::Event(note)]).await.unwrap();
    let expected = archive.events(0, 10).await.unwrap();
    assert_eq!(emitted[0], expected[1]);
    assert_eq!(archive.latest_events(2).await.unwrap(), expected[1..]);
    assert_eq!(
        expected[1]["payload"]["sweep"]["levels"][1]
            .as_f64()
            .unwrap()
            .to_bits(),
        (-0.0_f64).to_bits()
    );
    assert_eq!(
        expected[1]["payload"]["sweep"]["vendor"]["$serde_json::private::Number"],
        "literal"
    );
    assert_eq!(
        expected[1]["payload"]["sweep"]["interval"]["started_at"],
        "2026-11-11T12:00:00.123456Z"
    );
    let export = ori_archive::SnapshotExport::open_live(
        archive.clone(),
        ori_archive::SnapshotExportQuery {
            session_id: "fixture-сессия".into(),
            stream_id: Some("spectrum/one".into()),
        },
        Default::default(),
    )
    .unwrap();
    let record = export.next_record().await.unwrap().unwrap().value;
    for field in [
        "id",
        "session_id",
        "stream_id",
        "frequencies_hz",
        "levels",
        "level_unit",
        "settings",
    ] {
        assert_eq!(record[field], expected[1]["payload"]["sweep"][field]);
    }
    assert_eq!(
        record["levels"][1].as_f64().unwrap().to_bits(),
        (-0.0_f64).to_bits()
    );
    assert!(export.next_record().await.unwrap().is_none());
    export.close().await.unwrap();
    inspect(&live, 7);
    let bundle = temp.path().join("bundle");
    let restored = temp.path().join("restored");
    let converted = temp.path().join("converted");
    let portable = temp.path().join("portable");
    assert!(
        fs::metadata(temp.path().join("live.sqlite-wal"))
            .unwrap()
            .len()
            > 0
    );
    archive.backup(&bundle).await.unwrap();
    assert_eq!(verify_backup(&bundle).unwrap()["archive_schema"], 7);
    restore_backup(&bundle, &restored).unwrap();
    materialize_archive(&live, &converted).unwrap();
    archive.export_portable(&portable).await.unwrap();
    archive.close().await.unwrap();
    for (path, version) in [(&live, 7), (&restored, 7), (&portable, 6), (&converted, 6)] {
        inspect(path, version);
        let reader = Archive::open(ArchiveConfig::new(path)).unwrap();
        assert_eq!(reader.events(0, 10).await.unwrap(), expected);
        reader.close().await.unwrap();
    }
    let read = python(
        r#"
import json, struct, sys
from radioinspector.session_store import SQLiteSessionStore
from radioinspector.contracts import to_primitive
db = SQLiteSessionStore(sys.argv[1])
events = [to_primitive(e) for e in db.events(after=1)]
assert struct.pack('>d',events[0]['payload']['sweep']['levels'][1]).hex() == '8000000000000000'
print(json.dumps(events))
db.close()
"#,
        &[&live],
    );
    assert_eq!(
        ori_archive::parse_json_value(&read, 65536).unwrap(),
        json!(expected[1..])
    );
    assert!(materialize_archive(&live, &live).is_err());
    assert!(init_compact_archive(&live).await.is_err());
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn python_source_round_trips_and_corruption_is_rejected_before_publication() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("python.sqlite");
    let output = python(
        r#"
import json, sys
from radioinspector.session_store import SQLiteSessionStore
from tests.test_compact_panorama_events import record
store = SQLiteSessionStore(sys.argv[1], compact_sweep_events=True)
print(json.dumps(record(store)))
store.close()
"#,
        &[&path],
    );
    let expected = ori_archive::parse_json_value(&output, 65536).unwrap();
    let legacy = temp.path().join("legacy");
    materialize_archive(&path, &legacy).unwrap();
    for input in [&legacy, &path] {
        let reader = Archive::open(ArchiveConfig::new(input)).unwrap();
        assert_eq!(json!(reader.events(0, 10).await.unwrap()), expected);
        reader.close().await.unwrap();
    }
    let original = fs::read(&legacy).unwrap();
    let mut attempted = ArchiveConfig::new(&legacy);
    attempted.compact_sweep_events = true;
    assert!(Archive::open(attempted).is_err());
    assert_eq!(fs::read(&legacy).unwrap(), original);
    for (index, change) in [
        "UPDATE events SET ori_sweep_ref_v1='missing' WHERE sequence=1",
        "INSERT INTO sweeps SELECT 'other-sweep',session_id,stream_id,started_at,ended_at,frequencies_json,levels_json,level_unit,settings_json FROM sweeps;
         UPDATE events SET ori_sweep_ref_v1='other-sweep' WHERE sequence=1",
        "UPDATE events SET ori_sweep_ref_v1=NULL WHERE sequence=1",
        "UPDATE events SET ori_sweep_ref_v1=NULL, payload_json=char(32,9,10,13)||payload_json WHERE sequence=1",
        "UPDATE sweeps SET levels_json='[1,2,3,4]'",
        "UPDATE events SET event_type='other' WHERE sequence=1",
    ].iter().enumerate() {
        let source = temp.path().join(format!("bad-{index}"));
        fs::copy(&path,&source).unwrap();
        let db = Connection::open(&source).unwrap();
        db.execute_batch("PRAGMA foreign_keys=OFF").unwrap();
        db.execute_batch(change).unwrap();
        drop(db);
        let reader = Archive::open(ArchiveConfig::new(&source)).unwrap();
        assert!(reader.events(0,10).await.is_err());
        assert!(reader.latest_events(10).await.is_err());
        let backup = temp.path().join("rejected-backup");
        assert!(reader.backup(&backup).await.is_err());
        assert!(!backup.exists());
        reader.close().await.unwrap();
        let dest = temp.path().join("rejected");
        assert!(materialize_archive(&source,&dest).is_err());
        assert!(!dest.exists());
    }
    let fresh = temp.path().join("fresh");
    fs::write(temp.path().join("fresh-wal"), b"existing").unwrap();
    assert!(materialize_archive(&path, &fresh).is_err());
    assert!(init_compact_archive(&fresh).await.is_err());
    assert!(!fresh.exists());
}
