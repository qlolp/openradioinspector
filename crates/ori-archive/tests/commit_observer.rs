use ori_archive::{Archive, ArchiveConfig, CommitObserver};
use serde_json::Value;
use std::sync::{Arc, Mutex};

#[derive(Debug)]
struct Observer {
    path: std::path::PathBuf,
    calls: Mutex<Vec<(usize, i64)>>,
}
impl CommitObserver for Observer {
    fn committed(&self, items: &[Value], _events: &[Value]) {
        let db = rusqlite::Connection::open(&self.path).unwrap();
        let visible: i64 = db
            .query_row("SELECT count(*) FROM sweeps", [], |row| row.get(0))
            .unwrap();
        self.calls.lock().unwrap().push((items.len(), visible));
    }
}
#[tokio::test]
async fn observer_sees_committed_sources_and_never_a_rejected_batch() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("archive.sqlite");
    let observer = Arc::new(Observer {
        path: path.clone(),
        calls: Mutex::new(Vec::new()),
    });
    let mut config = ArchiveConfig::new(path);
    config.commit_observer = Some(observer.clone());
    let archive = Archive::open(config).unwrap();
    let fixture: Value = serde_json::from_str(include_str!(
        "../../../contracts/fixtures/measurement-v1.json"
    ))
    .unwrap();
    let mut session = fixture["session"].clone();
    session["created_at"] = serde_json::json!("2026-11-11T12:00:00.123456Z");
    archive.create_session(session).await.unwrap();
    observer.calls.lock().unwrap().clear();
    archive
        .append_sweeps(vec![fixture["sweep"].clone()])
        .await
        .unwrap();
    assert_eq!(*observer.calls.lock().unwrap(), vec![(1, 1)]);
    assert!(
        archive
            .append_sweeps(vec![fixture["sweep"].clone()])
            .await
            .is_err()
    );
    assert_eq!(*observer.calls.lock().unwrap(), vec![(1, 1)]);
    archive.close().await.unwrap();
}
