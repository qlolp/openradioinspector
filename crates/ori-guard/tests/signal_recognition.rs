mod common;

use common::*;
use ori_archive::{Archive, ArchiveConfig, MonitoringQuery, restore_backup};
use ori_guard::band_plan;
use serde_json::{Value, json};

async fn project(h: &Harness, name: &str) -> String {
    h.service
        .create(json!({
            "name": name, "site": "Synthetic only", "session_id": fixture()["session"]["id"]
        }))
        .await
        .unwrap()["id"]
        .as_str()
        .unwrap()
        .to_owned()
}

fn observed_event(id: &str, project: &str, sweep: &Value, frequency: u64) -> Value {
    json!({
        "id": id, "project_id": project, "created_at": sweep["interval"]["started_at"],
        "first_seen": sweep["interval"]["started_at"], "last_seen": sweep["interval"]["ended_at"],
        "center_frequency_hz": frequency, "start_frequency_hz": frequency - 10_000,
        "end_frequency_hz": frequency + 10_000, "peak_level": -61.25,
        "kind": "new_signal", "state": "open", "disposition": "unreviewed",
        "source_refs": [{
            "session_id": sweep["session_id"], "stream_id": sweep["stream_id"], "sweep_id": sweep["id"]
        }]
    })
}

#[test]
fn russian_band_plan_filters_and_keeps_overlapping_technology_hypotheses() {
    let data = band_plan(2_420_000_000, 2_420_000_000).unwrap();
    assert_eq!(data["profile"], "ru");
    assert_eq!(data["version"], 1);
    let items = data["items"].as_array().unwrap();
    assert!(
        items
            .iter()
            .any(|x| x["kinds"].as_array().unwrap().contains(&json!("wifi")))
    );
    assert!(
        items
            .iter()
            .any(|x| x["kinds"].as_array().unwrap().contains(&json!("bluetooth")))
    );
    for item in items {
        assert!(item["start_hz"].as_u64().unwrap() <= 2_420_000_000);
        assert!(item["end_hz"].as_u64().unwrap() >= 2_420_000_000);
        assert!(item["source_url"].as_str().unwrap().starts_with("https://"));
    }
    assert!(band_plan(2_420_000_001, 2_420_000_000).is_err());
}

#[tokio::test]
async fn confirmed_catalog_updates_in_place_without_changing_guard_disposition() {
    let h = Harness::new().await;
    let id = project(&h, "Catalog").await;
    let mut sweep = fixture()["sweeps"][0].clone();
    sweep["levels"][1] = json!(-61.25);
    h.archive.append_sweeps(vec![sweep.clone()]).await.unwrap();
    let frequency = sweep["frequencies_hz"][1].as_u64().unwrap();
    let event = observed_event("early", &id, &sweep, frequency);
    h.archive
        .put_monitoring("event", event.clone())
        .await
        .unwrap();

    let initial = h
        .service
        .event_with_recognition(&id, "early")
        .await
        .unwrap();
    assert_eq!(initial["recognition"]["state"], "new");
    let stored = h
        .service
        .save_known_signal(
            &id,
            json!({
                "event_id":"early", "name":"Known carrier", "kind":"broadcast"
            }),
        )
        .await
        .unwrap();
    let updated = h
        .service
        .save_known_signal(
            &id,
            json!({
                "event_id":"early", "name":"Verified carrier", "kind":"broadcast"
            }),
        )
        .await
        .unwrap();
    assert_eq!(updated["id"], stored["id"]);
    assert_eq!(updated["created_at"], stored["created_at"]);
    assert_eq!(updated["source_refs"][0]["sweep_id"], sweep["id"]);
    assert_eq!(updated["peak_level"], -61.25);
    assert_eq!(
        h.archive.monitoring("event", "early").await.unwrap(),
        Some(event)
    );
    assert!(
        h.archive
            .list_monitoring("allowed", MonitoringQuery::default())
            .await
            .unwrap()
            .is_empty()
    );

    let later = observed_event("later", &id, &sweep, frequency);
    h.archive.put_monitoring("event", later).await.unwrap();
    let found = h
        .service
        .event_with_recognition(&id, "later")
        .await
        .unwrap();
    assert_eq!(found["recognition"]["state"], "similar");
    assert_eq!(
        found["recognition"]["known_matches"][0]["name"],
        "Verified carrier"
    );
    assert_eq!(found["disposition"], "unreviewed");
    assert_eq!(
        h.service.recognized_events(&id, "", 10).await.unwrap()["items"][0]["recognition"]["state"],
        "similar"
    );

    let page = h.service.known_signals(&id, "", 1).await.unwrap();
    assert_eq!(page["items"].as_array().unwrap().len(), 1);
    assert_eq!(page["next_cursor"], Value::Null);
    assert!(
        h.service
            .save_known_signal(
                &id,
                json!({"event_id":"early", "name":"", "kind":"broadcast"})
            )
            .await
            .is_err()
    );
    assert!(
        h.service
            .save_known_signal(
                &id,
                json!({"event_id":"early", "name":"Claim", "kind":"unsupported"})
            )
            .await
            .is_err()
    );
    assert!(h.service.known_signals(&id, "unowned", 1).await.is_err());
    let directory = tempfile::tempdir().unwrap();
    let backup = directory.path().join("backup");
    h.archive.backup(&backup).await.unwrap();
    let restored = directory.path().join("restored.sqlite");
    restore_backup(&backup, &restored).unwrap();
    let archive = Archive::open(ArchiveConfig::new(&restored)).unwrap();
    assert_eq!(
        archive.monitoring("known-signal", "early").await.unwrap(),
        Some(updated)
    );
    h.close().await;
}

#[tokio::test]
async fn catalog_matches_older_compatible_widths_within_the_same_scan_revision() {
    let h = Harness::new().await;
    let id = project(&h, "Catalog").await;
    let mut sweep = fixture()["sweeps"][0].clone();
    sweep["levels"][1] = json!(-61.25);
    sweep["settings"]["scan_plan_id"] = json!("plan");
    sweep["settings"]["scan_band_id"] = json!("band");
    sweep["settings"]["scan_plan_revision"] = json!(1);
    h.archive.append_sweeps(vec![sweep.clone()]).await.unwrap();
    let frequency = sweep["frequencies_hz"][1].as_u64().unwrap();
    for index in 0..7 {
        let event_id = format!("signal-{index}");
        let mut event = observed_event(&event_id, &id, &sweep, frequency);
        event["scan_plan_id"] = json!("plan");
        event["scan_band_id"] = json!("band");
        event["scan_plan_revision"] = json!(1);
        if index == 0 {
            event["start_frequency_hz"] = json!(frequency);
            event["end_frequency_hz"] = json!(frequency);
            event["first_bin"] = json!(1);
            event["last_bin"] = json!(1);
        } else {
            event["end_frequency_hz"] = json!(frequency + 60_000);
        }
        h.archive.put_monitoring("event", event).await.unwrap();
        h.service
            .save_known_signal(
                &id,
                json!({"event_id": event_id, "name": event_id, "kind": "other"}),
            )
            .await
            .unwrap();
    }
    let mut later = observed_event("later", &id, &sweep, frequency);
    later["scan_plan_id"] = json!("plan");
    later["scan_band_id"] = json!("band");
    later["scan_plan_revision"] = json!(1);
    later["start_frequency_hz"] = json!(frequency);
    later["first_bin"] = json!(1);
    later["last_bin"] = json!(2);
    h.archive
        .put_monitoring("event", later.clone())
        .await
        .unwrap();
    let matched = h
        .service
        .event_with_recognition(&id, "later")
        .await
        .unwrap();
    assert_eq!(matched["recognition"]["known_matches"][0]["id"], "signal-0");
    later["scan_plan_revision"] = json!(2);
    h.archive.put_monitoring("event", later).await.unwrap();
    let unmatched = h
        .service
        .event_with_recognition(&id, "later")
        .await
        .unwrap();
    assert_eq!(unmatched["recognition"]["state"], "new");
    h.close().await;
}

#[tokio::test]
async fn catalog_requires_an_exact_measured_bin_and_only_shows_the_paired_decoder() {
    let h = Harness::new().await;
    let id = project(&h, "Evidence").await;
    let mut sweep = fixture()["sweeps"][0].clone();
    sweep["levels"][1] = json!(-61.25);
    h.archive.append_sweeps(vec![sweep.clone()]).await.unwrap();
    let frequency = sweep["frequencies_hz"][1].as_u64().unwrap();
    let event = observed_event("off-grid", &id, &sweep, frequency + 1);
    h.archive.put_monitoring("event", event).await.unwrap();
    assert!(
        h.service
            .save_known_signal(
                &id,
                json!({
                    "event_id": "off-grid", "name": "Off grid", "kind": "other"
                })
            )
            .await
            .is_err()
    );

    let mut event = observed_event("verified", &id, &sweep, frequency);
    event["analysis"] = json!({
        "verification": "observed_both",
        "classifications": [{"observation_id":"unpaired", "source":"receiver_RX", "mode":"DMR"}],
        "verification_evidence": {
            "simulated": false,
            "receiver": {"observation_id":"paired", "decoder_source":"receiver_RX", "decoded_mode":"DSTAR"}
        }
    });
    for simulated in [false, true] {
        event["analysis"]["verification_evidence"]["simulated"] = json!(simulated);
        h.archive
            .put_monitoring("event", event.clone())
            .await
            .unwrap();
        let detail = h
            .service
            .event_with_recognition(&id, "verified")
            .await
            .unwrap();
        let expected = if simulated {
            Value::Null
        } else {
            json!("DSTAR")
        };
        assert_eq!(
            detail["recognition"]["observed_features"]["confirmed_receiver_mode"],
            expected
        );
    }
    h.close().await;
}

#[tokio::test]
async fn catalog_resolves_an_old_peak_after_its_embedded_reference_rolls_over() {
    let h = Harness::new().await;
    let id = project(&h, "Peak history").await;
    let f = fixture();
    let mut peak = f["sweeps"][0].clone();
    let mut later = f["sweeps"][1].clone();
    peak["levels"][1] = json!(-61.25);
    later["levels"][1] = json!(-70.0);
    h.archive
        .append_sweeps(vec![peak.clone(), later.clone()])
        .await
        .unwrap();
    let mut event = observed_event(
        "rolled",
        &id,
        &later,
        peak["frequencies_hz"][1].as_u64().unwrap(),
    );
    event["first_seen"] = peak["interval"]["started_at"].clone();
    h.archive.put_monitoring("event", event).await.unwrap();
    h.archive.put_monitoring("event-source", json!({
        "id":"rolled.peak", "event_id":"rolled", "project_id":id,
        "created_at": peak["interval"]["started_at"],
        "session_id":peak["session_id"], "stream_id":peak["stream_id"], "sweep_id":peak["id"]
    })).await.unwrap();
    let known = h
        .service
        .save_known_signal(
            &id,
            json!({
                "event_id": "rolled", "name": "Measured peak", "kind": "other"
            }),
        )
        .await
        .unwrap();
    assert_eq!(known["source_refs"][0]["sweep_id"], peak["id"]);
    assert_eq!(known["peak_level"], -61.25);
    h.close().await;
}
