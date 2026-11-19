use ori_archive::{Archive, ArchiveConfig, EventDraft, Mutation};
use serde_json::json;

#[tokio::test]
async fn receipt_budget_counts_exact_utf8_bytes_and_keeps_the_durable_event() {
    let stamp = "2026-01-01T00:00:00.000000Z";
    let payload = json!({"annotation":"Радио\n\"A\"", "levels":[-0.0, -88.125]});
    let expected = json!({"sequence":1,"event_type":"annotation","occurred_at":stamp,
        "session_id":null,"payload":payload});
    let length = serde_json::to_vec(&expected).unwrap().len();
    for budget in [length - 1, length] {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("events.sqlite");
        let mut config = ArchiveConfig::new(&path);
        config.max_response_bytes = budget;
        let archive = Archive::open(config).unwrap();
        let result = archive
            .apply(vec![Mutation::Event(EventDraft {
                event_type: "annotation".into(),
                occurred_at: stamp.into(),
                session_id: None,
                payload: payload.clone(),
            })])
            .await
            .unwrap();
        assert_eq!(result.events_complete, budget == length);
        assert_eq!(
            result.events.first(),
            (budget == length).then_some(&expected)
        );
        assert_eq!(result.last_sequence, Some(1));
        archive.close().await.unwrap();
        let reader = Archive::open(ArchiveConfig::new(&path)).unwrap();
        assert_eq!(reader.events(0, 1).await.unwrap(), vec![expected.clone()]);
        reader.close().await.unwrap();
    }
}

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
