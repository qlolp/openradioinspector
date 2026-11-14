//! Scale fixture contains synthetic two-point sources, never user measurements.
use ori_archive::{Archive, ArchiveConfig, SweepQuery};
use serde_json::json;

async fn indexed_history(count: i64) {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("archive.sqlite");
    let archive = Archive::open(ArchiveConfig::new(&path)).unwrap();
    archive.create_session(json!({"id":"history","name":"Synthetic indexed history","created_at":"2026-11-11T00:00:00Z","streams":[{"id":"stream","device":"synthetic","role":"spectrum_analyzer","level_unit":"dBm"}]})).await.unwrap();
    archive.close().await.unwrap();
    {
        let mut db = rusqlite::Connection::open(&path).unwrap();
        let tx = db.transaction().unwrap();
        tx.execute("WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x+1<?) INSERT INTO sweeps SELECT printf('source-%07d',x),'history','stream',printf('2026-11-11T12:00:00.%06dZ',x),printf('2026-11-11T12:00:01.%06dZ',x),'[1,2]','[-1.0,-0.0]','dBm','{}' FROM n",[count]).unwrap();
        tx.execute_batch("INSERT INTO monitoring_sweep_index SELECT id,session_id,started_at,ended_at,1,2 FROM sweeps;INSERT INTO sweep_ingestion(sweep_id,session_id,stream_id) SELECT id,session_id,stream_id FROM sweeps ORDER BY rowid").unwrap();
        tx.commit().unwrap();
        let plan=db.prepare("EXPLAIN QUERY PLAN SELECT m.sweep_id FROM monitoring_sweep_index m INDEXED BY monitoring_sweep_index_scope_time CROSS JOIN sweeps s CROSS JOIN sweep_ingestion i WHERE m.session_id=? AND i.sequence<=? AND s.id=m.sweep_id AND i.sweep_id=m.sweep_id ORDER BY m.started_at DESC LIMIT 33").unwrap().query_map(rusqlite::params!["history",count],|row|row.get::<_,String>(3)).unwrap().collect::<std::result::Result<Vec<_>,_>>().unwrap();
        assert!(
            plan.iter().any(|line| line.contains("SEARCH m USING")
                && line.contains("monitoring_sweep_index_scope_time")),
            "{plan:?}"
        );
        assert!(!plan.iter().any(|line| line.contains("SCAN s")), "{plan:?}");
    }
    let archive = Archive::open(ArchiveConfig::new(path)).unwrap();
    let mut query = SweepQuery::session("history");
    query.limit = 32;
    let first = archive
        .sweep_summaries(query.clone(), count as u64)
        .await
        .unwrap();
    assert_eq!(first.items.len(), 32);
    assert_eq!(first.items[0]["id"], format!("source-{:07}", count - 1));
    query.after = first.next_cursor;
    let next = archive.sweep_summaries(query, count as u64).await.unwrap();
    assert_eq!(next.items[0]["id"], format!("source-{:07}", count - 33));
    archive.close().await.unwrap();
}
#[tokio::test]
async fn indexed_ten_thousand_source_history_uses_bounded_pages() {
    indexed_history(10_000).await;
}
#[tokio::test]
async fn indexed_million_source_history_uses_bounded_pages() {
    indexed_history(1_000_000).await;
}
