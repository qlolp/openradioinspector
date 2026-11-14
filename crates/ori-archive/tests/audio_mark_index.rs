use ori_archive::{Archive, ArchiveConfig};
use rusqlite::{Connection, params};
use std::sync::{
    Arc,
    atomic::{AtomicUsize, Ordering},
};

#[tokio::test]
async fn old_archive_migration_seeks_only_the_requested_event_marks() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("old.sqlite");
    std::fs::copy(
        concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/tests/fixtures/legacy_v5_utf16.sqlite"
        ),
        &path,
    )
    .unwrap();
    let before;
    {
        let mut db = Connection::open(&path).unwrap();
        before = db
            .query_row(
                "SELECT kind,id,payload FROM monitoring_records ORDER BY kind,id LIMIT 1",
                [],
                |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, String>(2)?,
                    ))
                },
            )
            .unwrap();
        let tx = db.transaction().unwrap();
        tx.execute("INSERT INTO monitoring_records VALUES('event','wanted-event','wanted-project','2026-11-14T00:00:00Z','{}')",[]).unwrap();
        {
            let mut statement=tx.prepare("INSERT INTO monitoring_records VALUES('audio-mark',?,'other-project','2026-11-14T00:00:00Z','{\"event_id\":\"other-event\"}')").unwrap();
            for id in 0..100_000 {
                statement.execute([format!("a{id:08}")]).unwrap();
            }
        }
        tx.execute("INSERT INTO monitoring_records VALUES('audio-mark','z-target','wanted-project','2026-11-14T00:00:00Z','{\"event_id\":\"wanted-event\"}')",[]).unwrap();
        tx.commit().unwrap();
    }
    let archive = Archive::open(ArchiveConfig::new(&path)).unwrap();
    archive.close().await.unwrap();
    let mut db = Connection::open(&path).unwrap();
    const QUERY: &str = "SELECT payload FROM monitoring_records WHERE kind=? AND project_id=? AND json_extract(payload,'$.event_id')=? AND id>? ORDER BY id LIMIT ?";
    let arguments = params!["audio-mark", "wanted-project", "wanted-event", "", 2];
    let instructions = Arc::new(AtomicUsize::new(0));
    let count = instructions.clone();
    db.progress_handler(
        1,
        Some(move || {
            count.fetch_add(1, Ordering::SeqCst);
            false
        }),
    )
    .unwrap();
    let rows = db
        .prepare(QUERY)
        .unwrap()
        .query_map(arguments, |row| row.get::<_, String>(0))
        .unwrap()
        .collect::<Result<Vec<_>, _>>()
        .unwrap();
    db.progress_handler(0, None::<fn() -> bool>).unwrap();
    assert_eq!(rows.len(), 1);
    assert!(
        instructions.load(Ordering::SeqCst) < 1000,
        "one scoped page used {} instructions",
        instructions.load(Ordering::SeqCst)
    );
    let plan = db
        .prepare(&format!("EXPLAIN QUERY PLAN {QUERY}"))
        .unwrap()
        .query_map(arguments, |row| row.get::<_, String>(3))
        .unwrap()
        .collect::<Result<Vec<_>, _>>()
        .unwrap()
        .join("; ");
    assert!(plan.contains("ori_monitoring_event_cursor"), "{plan}");
    assert_eq!(
        db.query_row(
            "SELECT version FROM ori_archive_query_schema WHERE singleton=1",
            [],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        1
    );
    assert_eq!(
        db.query_row(
            "SELECT payload FROM monitoring_records WHERE kind=? AND id=?",
            params![before.0, before.1],
            |row| row.get::<_, String>(0)
        )
        .unwrap(),
        before.2
    );
}
