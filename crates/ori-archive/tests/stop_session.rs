use ori_archive::{Archive, ArchiveConfig, Mutation};
use serde_json::json;

#[tokio::test]
async fn stopping_more_than_256_jobs_is_atomic_and_bounded_ack_reports_the_full_persisted_event_range()
 {
    let dir = tempfile::tempdir().unwrap();
    let archive = Archive::open(ArchiveConfig::new(dir.path().join("archive.sqlite"))).unwrap();
    archive.create_session(json!({"id":"session","name":"Synthetic stop graph","state":"running","created_at":"2026-11-11T00:00:00Z","started_at":"2026-11-11T00:00:01Z","streams":[{"id":"analyzer","role":"spectrum_analyzer","device":"synthetic","level_unit":"dBm"},{"id":"receiver","role":"receiver","device":"synthetic","level_unit":"s_meter_count"}]})).await.unwrap();
    let candidate = json!({"id":"candidate","session_id":"session","source_stream_id":"analyzer","interval":{"started_at":"2026-11-11T00:00:02Z","ended_at":"2026-11-11T00:00:03Z"},"center_frequency_hz":100000000,"level_unit":"dBm","state":"investigating"});
    for batch in 0..3 {
        let mutations=(0..100).map(|i|Mutation::Investigation{session_id:"session".into(),value:json!({"id":format!("job-{}",batch*100+i),"candidate_id":"candidate","target_stream_id":"receiver","target_frequency_hz":100000000,"state":"queued","created_at":"2026-11-11T00:00:04Z"}),candidate:candidate.clone(),event:None}).collect();
        archive.apply(mutations).await.unwrap();
    }
    let session = archive.get_session("session").await.unwrap().unwrap();
    let mut notices = archive.subscribe_commits();
    let result = archive
        .stop_session(session, "2026-11-11T00:01:00Z")
        .await
        .unwrap();
    assert_eq!(result.items[0]["state"], "stopped");
    assert_eq!(result.first_sequence, Some(2));
    assert_eq!(result.last_sequence, Some(302));
    assert!(!result.events_complete);
    assert_eq!(result.events.len(), 256);
    let notice = notices.recv().await.unwrap();
    assert_eq!(notice.last_sequence, 302);
    assert_eq!(archive.events(1, 1000).await.unwrap().len(), 301);
    assert!(
        archive
            .list_open_investigations(1000, None)
            .await
            .unwrap()
            .is_empty()
    );
    assert_eq!(
        archive.get_candidate("candidate").await.unwrap().unwrap()["state"],
        "queued"
    );
    let current = archive.get_session("session").await.unwrap().unwrap();
    assert!(
        archive
            .stop_session(current, "2026-11-11T00:02:00Z")
            .await
            .is_err()
    );
    assert_eq!(
        archive.registry_counts().await.unwrap()["last_event_id"],
        302
    );
    archive.close().await.unwrap();
}
