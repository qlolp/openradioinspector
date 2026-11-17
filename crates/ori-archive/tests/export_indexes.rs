use ori_archive::{Archive, ArchiveConfig, ArchiveError};
use rusqlite::{Connection, functions::FunctionFlags, params};

fn raw_rows(db: &Connection) -> Vec<Vec<String>> {
    db.prepare("SELECT id,session_id,stream_id,started_at,ended_at,frequencies_json,levels_json,level_unit,settings_json FROM sweeps ORDER BY id")
        .unwrap().query_map([], |row| (0..9).map(|i| row.get(i)).collect())
        .unwrap().collect::<rusqlite::Result<_>>().unwrap()
}

#[tokio::test]
async fn legacy_export_is_covered_without_changing_raw_values_or_database_encoding() {
    let root = tempfile::tempdir().unwrap();
    let path = root.path().join("legacy.sqlite");
    std::fs::copy(
        concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/tests/fixtures/legacy_v5_utf16.sqlite"
        ),
        &path,
    )
    .unwrap();
    let before = Connection::open(&path).unwrap();
    let original = raw_rows(&before);
    let encoding: String = before
        .query_row("PRAGMA encoding", [], |r| r.get(0))
        .unwrap();
    drop(before);
    let archive = Archive::open(ArchiveConfig::new(&path)).unwrap();
    archive.close().await.unwrap();
    let db = Connection::open(&path).unwrap();
    assert_eq!(raw_rows(&db), original);
    assert_eq!(
        db.query_row("PRAGMA encoding", [], |r| r.get::<_, String>(0))
            .unwrap(),
        encoding
    );
    assert_eq!(
        db.query_row(
            "SELECT version FROM ori_export_index_schema WHERE singleton=1",
            [],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        1
    );
    db.create_scalar_function("context_utc", 1, FunctionFlags::SQLITE_UTF8, |c| {
        c.get::<String>(0)
    })
    .unwrap();
    db.create_scalar_function("context_utf8", 1, FunctionFlags::SQLITE_UTF8, |c| {
        Ok(c.get::<String>(0)?.into_bytes())
    })
    .unwrap();
    for (table, index) in [
        ("sweeps", "ori_sweeps_export_v1"),
        ("observations", "ori_observations_export_v1"),
    ] {
        for stream in [None, Some("spectrum/one")] {
            let sql = format!(
                "EXPLAIN QUERY PLAN SELECT id FROM {table} WHERE session_id=?1 AND (?2 IS NULL OR stream_id=?2) ORDER BY context_utc(started_at),context_utf8(id)"
            );
            let plan = db
                .prepare(&sql)
                .unwrap()
                .query_map(params!["fixture-сессия", stream], |r| {
                    r.get::<_, String>(3)
                })
                .unwrap()
                .collect::<rusqlite::Result<Vec<_>>>()
                .unwrap()
                .join("\n");
            assert!(
                plan.contains(&format!("COVERING INDEX {index}")),
                "{table}/{stream:?}: {plan}"
            );
        }
    }
}

#[tokio::test]
async fn future_export_index_version_is_refused_before_any_archive_write() {
    let root = tempfile::tempdir().unwrap();
    let path = root.path().join("future.sqlite");
    let archive = Archive::open(ArchiveConfig::new(&path)).unwrap();
    archive.close().await.unwrap();
    let db = Connection::open(&path).unwrap();
    db.execute("UPDATE ori_export_index_schema SET version=2", [])
        .unwrap();
    drop(db);
    let before = std::fs::read(&path).unwrap();
    assert!(matches!(
        Archive::open(ArchiveConfig::new(&path)),
        Err(ArchiveError::UnsupportedSchema {
            domain: "export_indexes",
            version: 2
        })
    ));
    assert_eq!(std::fs::read(path).unwrap(), before);
}

#[tokio::test]
async fn export_indexes_allow_ordinary_sqlite_writers_without_application_functions() {
    let root = tempfile::tempdir().unwrap();
    let path = root.path().join("sqlite-writer.sqlite");
    let archive = Archive::open(ArchiveConfig::new(&path)).unwrap();
    archive.close().await.unwrap();
    let db = Connection::open(&path).unwrap();
    assert_eq!(
        db.query_row("SELECT version FROM ori_export_index_schema", [], |r| r
            .get::<_, i64>(0))
            .unwrap(),
        1
    );
    // Preparing maintenance of both indexes must not require Rust/Python UDFs.
    db.prepare("INSERT INTO sweeps(id,session_id,stream_id,started_at,ended_at,frequencies_json,levels_json,level_unit,settings_json) VALUES (?,?,?,?,?,?,?,?,?)").unwrap();
    db.prepare("INSERT INTO observations(id,session_id,stream_id,started_at,ended_at,frequency_hz,mode,level,level_unit,squelch_open,metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)").unwrap();
}
