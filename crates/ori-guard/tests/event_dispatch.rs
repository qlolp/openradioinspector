mod common;

use common::*;
use serde_json::{Value, json};
use std::time::Duration;

async fn seed_event(h: &Harness, id: &str, project: &Value) -> String {
    let f = fixture();
    let mut sweeps = f["sweeps"].as_array().unwrap()[20..23].to_vec();
    for (index, sweep) in sweeps.iter_mut().enumerate() {
        let second = 48 + index * 4;
        sweep["id"] = json!(format!("live/{index}"));
        sweep["session_id"] = project["active_session_id"].clone();
        sweep["stream_id"] = project["analyzer_stream_id"].clone();
        sweep["interval"] = json!({
            "started_at": format!("2026-11-11T12:59:{second:02}.123456Z"),
            "ended_at": format!("2026-11-11T12:59:{:02}.123456Z", second + 1),
        });
    }
    h.archive.append_sweeps(sweeps).await.unwrap();
    h.service.analyze(id).await.unwrap()["event_ids"][0]
        .as_str()
        .unwrap()
        .to_owned()
}

#[tokio::test]
async fn dispatch_denies_a_previously_queued_event_after_its_deadline_or_operator_review() {
    for operator_review in [false, true] {
        let h = Harness::new().await;
        let id = h.prepared().await;
        let project = h.service.arm(&id, json!({})).await.unwrap();
        let event_id = seed_event(&h, &id, &project).await;
        let gate = TuneGate::new();
        *h.control.tune_gate.lock().unwrap() = Some(gate.clone());
        h.service.tick(&id).await.unwrap();
        tokio::time::timeout(Duration::from_secs(3), gate.entered.notified())
            .await
            .unwrap();
        let (state, reason, disposition) = if operator_review {
            h.service
                .review_event(
                    &id,
                    &event_id,
                    json!({
                        "disposition":"false_positive", "comment":"Operator reviewed"
                    }),
                )
                .await
                .unwrap();
            ("interrupted", "event_reviewed", "false_positive")
        } else {
            h.sources.advance(31);
            ("expired", "event_expired", "unreviewed")
        };
        gate.release.notify_one();
        let event = tokio::time::timeout(Duration::from_secs(3), async {
            loop {
                let event = h
                    .service
                    .get_record("event", &event_id, Some(&id))
                    .await
                    .unwrap();
                if event["analysis"]["state"] != "queued" {
                    break event;
                }
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .unwrap();
        assert_eq!(event["analysis"]["state"], state, "{event}");
        assert_eq!(event["analysis"]["verification_evidence"]["reason"], reason);
        assert_eq!(event["disposition"], disposition);
        assert!(
            !h.control
                .calls
                .lock()
                .unwrap()
                .iter()
                .any(|call| call.starts_with("tune:"))
        );
        h.close().await;
    }
}

#[tokio::test]
async fn a_new_peak_supersedes_a_queued_frequency_before_receiver_io() {
    let h = Harness::new().await;
    let id = h.prepared().await;
    let project = h.service.arm(&id, json!({})).await.unwrap();
    let event_id = seed_event(&h, &id, &project).await;
    let gate = TuneGate::new();
    *h.control.tune_gate.lock().unwrap() = Some(gate.clone());
    h.service.tick(&id).await.unwrap();
    tokio::time::timeout(Duration::from_secs(3), gate.entered.notified())
        .await
        .unwrap();

    let mut sweep = fixture()["sweeps"][22].clone();
    sweep["id"] = json!("live/stronger");
    sweep["session_id"] = project["active_session_id"].clone();
    sweep["stream_id"] = project["analyzer_stream_id"].clone();
    sweep["interval"] = json!({"started_at":"2026-11-11T13:00:00.123456Z","ended_at":"2026-11-11T13:00:01.123456Z"});
    sweep["levels"][4] = json!(-65.0);
    sweep["levels"][5] = json!(-55.0);
    *h.sources.now.lock().unwrap() = "2026-11-11T13:00:02.123456Z".parse().unwrap();
    h.archive.append_sweeps(vec![sweep]).await.unwrap();
    h.service.analyze(&id).await.unwrap();
    let updated = h
        .service
        .get_record("event", &event_id, Some(&id))
        .await
        .unwrap();
    *h.control.tune_gate.lock().unwrap() = None;
    gate.release.notify_one();
    assert_eq!(updated["center_frequency_hz"], 100_050_000, "{updated}");

    tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            h.service.tick(&id).await.unwrap();
            if h.control
                .calls
                .lock()
                .unwrap()
                .iter()
                .any(|call| call.starts_with("tune:"))
            {
                break;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap();
    let calls = h.control.calls.lock().unwrap().clone();
    assert!(
        calls.iter().any(|call| call.ends_with(":100050000:AUTO")),
        "{calls:?}"
    );
    assert!(
        calls.iter().all(|call| !call.ends_with(":100040000:AUTO")),
        "{calls:?}"
    );
    h.close().await;
}
