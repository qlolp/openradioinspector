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

#[tokio::test]
async fn receiver_catalog_validates_the_exact_raw_point_and_preserves_its_unit() {
    let h = Harness::new().await;
    let id = project(&h, "Receiver originals").await;
    let frequency = 100_000_000;
    let session = fixture()["session"].clone();
    let stream = session["streams"]
        .as_array()
        .unwrap()
        .iter()
        .find(|s| s["role"] == "receiver")
        .unwrap();
    let interval = json!({"started_at":"2026-11-11T10:00:00.000000Z","ended_at":"2026-11-11T10:00:00.150000Z"});
    let obs = json!({"id":"known-rx","session_id":session["id"],"stream_id":stream["id"],
        "frequency_hz":frequency,"interval":interval,"mode":"FM","level":44,"level_unit":"s_meter_count","squelch_open":true,
        "metadata":{"adapter":"ardv1-serial","receiver_profile":"ardv1-monitor-v1","receiver_firmware":"2504A",
        "scan_plan_key":"first-duty","scan_strategy_requested":"rf","scope_source":"sequential_receiver_readback",
        "scan_pass":2,"scan_index":0,"scan_total":2,"scan_start_hz":frequency,"scan_stop_hz":100010000,
        "scan_step_hz":10000,"settle_s":0.015,"scan_segment_index":0,"observed_frequency_hz":frequency,
        "requested_frequency_hz":frequency,"observed_mode":"FM","requested_mode":"FM","frequency_readback":true,
        "frequency_source":"receiver_RX","mode_source":"receiver_RX","raw_squelch_status":1,"raw_mode_status":"MD0F0",
        "simulated":false,"raw_receiver_status":"RX VFA RF0100.00000 ST010.00 MD0F0 LM441"}});
    h.archive
        .append_observations(vec![obs.clone()])
        .await
        .unwrap();
    let mut event = json!({"id":"count-event","project_id":id,"created_at":interval["started_at"],
        "first_seen":interval["started_at"],"last_seen":interval["ended_at"],
        "run_session_id":session["id"],"center_frequency_hz":frequency,"start_frequency_hz":frequency,
        "end_frequency_hz":frequency,"peak_level":44,"level_unit":"s_meter_count","scan_plan_id":"first-duty",
        "kind":"new_signal","source_count":3,"disposition":"unreviewed",
        "peak_source":{"session_id":session["id"],"stream_id":stream["id"],"scan_pass":2,
                       "frequency_hz":frequency,"observation_id":"known-rx","level":44,"level_unit":"s_meter_count","interval":interval}});
    h.archive
        .put_monitoring("event", event.clone())
        .await
        .unwrap();
    let request =
        json!({"event_id":"count-event","name":"Собственный приёмный образец","kind":"other"});
    let record = h
        .service
        .save_known_signal(&id, request.clone())
        .await
        .unwrap();
    assert_eq!(record["level_unit"], "s_meter_count");
    assert_eq!(record["source_refs"][0]["observation_id"], "known-rx");
    assert!(record["source_refs"][0].get("sweep_id").is_none());
    let viewed = h
        .service
        .event_with_recognition(&id, "count-event")
        .await
        .unwrap();
    assert_eq!(
        viewed["recognition"]["observed_features"]["source_observation_count"],
        3
    );
    assert!(viewed["recognition"]["observed_features"]["confirmed_receiver_mode"].is_null());
    assert_eq!(viewed["disposition"], "unreviewed");
    event["peak_level"] = json!(45);
    h.archive.put_monitoring("event", event).await.unwrap();
    assert!(h.service.save_known_signal(&id, request).await.is_err());
    h.close().await;
}
