mod common;
use common::*;
use ori_guard::ControlTarget;
use serde_json::json;

#[tokio::test]
async fn narrow_search_is_scoped_receive_only_and_marked_original_is_retained_after_return() {
    let h = Harness::new().await;
    let id = h.prepared().await;
    let p = h.service.arm(&id, json!({})).await.unwrap();
    let mut event = fixture()["first_event"].clone();
    event["id"] = json!("search-event");
    event["project_id"] = json!(id);
    event["run_session_id"] = p["active_session_id"].clone();
    h.archive.put_monitoring("event", event).await.unwrap();
    let lease = h
        .service
        .manual_acquire(&id, json!({"role":"analyzer"}))
        .await
        .unwrap();
    let search = h
        .service
        .search_start(
            &id,
            json!({"token":lease["token"],"event_id":"search-event","span_hz":20000}),
        )
        .await
        .unwrap();
    assert_eq!(search["coverage"], "narrow");
    assert_eq!(h.service.project(&id).await.unwrap()["state"], "searching");
    let search_sid = search["session_id"].as_str().unwrap();
    let search_stream = search["stream_id"].as_str().unwrap();
    let target = ControlTarget {
        session_id: search_sid.into(),
        stream_id: search_stream.into(),
    };
    assert!(h.service.authorize_control(&target, None).is_err());
    let s = h.archive.get_session(search_sid).await.unwrap().unwrap();
    assert_eq!(s["streams"].as_array().unwrap().len(), 1);
    assert_eq!(s["streams"][0]["settings"]["start_hz"], 100030000);
    assert_eq!(s["streams"][0]["settings"]["stop_hz"], 100050000);
    assert!(
        h.control
            .workers
            .lock()
            .unwrap()
            .iter()
            .any(|w| w.stream_id == p["receiver_stream_id"].as_str().unwrap() && w.alive)
    );
    let sweep = json!({"id":"narrow/source/%+α","session_id":search_sid,"stream_id":search_stream,"interval":{"started_at":fixture()["clock"],"ended_at":"2026-11-11T13:00:01.123456Z"},"frequencies_hz":[100030000,100040000,100050000],"levels":[-100.0,-80.0,-95.0],"level_unit":"dBm","settings":{"adapter":"synthetic","raw":"intact"}});
    h.archive.append_sweeps(vec![sweep.clone()]).await.unwrap();
    let status = h.service.search_status(&id).await.unwrap();
    assert_eq!(status["maximum_level"], -80.0);
    assert_eq!(status["series"][0]["frequency_hz"], 100040000);
    let mark = h
        .service
        .search_mark(&id, json!({"token":lease["token"],"label":"Corner α"}))
        .await
        .unwrap();
    assert_eq!(mark["sweep_id"], "narrow/source/%+α");
    assert_eq!(mark["level"], -80.0);
    let mut fresh = fixture()["sweeps"][0].clone();
    fresh["id"] = json!("return-after-search");
    fresh["interval"] =
        json!({"started_at":fixture()["clock"],"ended_at":"2026-11-11T13:00:01.123456Z"});
    *h.control.sweep_on_start.lock().unwrap() = Some((h.archive.clone(), fresh));
    h.service
        .manual_return(&id, json!({"token":lease["token"]}))
        .await
        .unwrap();
    tokio::time::timeout(std::time::Duration::from_secs(3), async {
        loop {
            let project = h.service.project(&id).await.unwrap();
            if project["coverage"] == "wide" {
                break;
            }
            assert_ne!(project["state"], "error", "{project}");
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    assert_eq!(
        h.archive.get_session(search_sid).await.unwrap().unwrap()["state"],
        "stopped"
    );
    assert_eq!(
        h.service
            .history_original(&id, "narrow/source/%+α")
            .await
            .unwrap(),
        sweep
    );
    assert!(h.service.authorize_control(&target, None).is_ok());
    h.close().await;
}

#[tokio::test]
async fn expired_manual_analyzer_keeps_pause_and_rejects_new_search_or_marks() {
    let h = Harness::new().await;
    let id = h.prepared().await;
    h.service.arm(&id, json!({})).await.unwrap();
    let lease = h
        .service
        .manual_acquire(&id, json!({"role":"analyzer"}))
        .await
        .unwrap();
    h.sources.advance(31);
    assert!(
        h.service
            .search_start(
                &id,
                json!({"token":lease["token"],"event_id":"absent","span_hz":20000})
            )
            .await
            .is_err()
    );
    assert!(
        h.service
            .search_mark(&id, json!({"token":lease["token"],"label":"Never"}))
            .await
            .is_err()
    );
    assert_eq!(h.service.project(&id).await.unwrap()["coverage"], "paused");
    h.close().await;
}
