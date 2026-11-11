mod common;
use common::*;
use ori_guard::ControlTarget;
use serde_json::{Value, json};

fn operations_fixture() -> Value {
    ori_contracts::parse_json_value(
        include_bytes!("../../../contracts/fixtures/guard-operations-v1.json"),
        1024 * 1024,
    )
    .unwrap()
}

#[tokio::test]
async fn python_settings_outbox_dedup_retry_ack_and_exact_transport_body() {
    let h = Harness::new().await;
    let f = operations_fixture();
    let p = h
        .service
        .create(
            json!({"name":"Operations","site":"Fixture","session_id":fixture()["session"]["id"]}),
        )
        .await
        .unwrap();
    let id = p["id"].as_str().unwrap();
    assert_eq!(h.service.settings(id).await.unwrap(), f["defaults"]);
    assert_eq!(
        h.service
            .update_settings(id, f["update"].clone())
            .await
            .unwrap(),
        f["settings"]
    );
    assert_eq!(
        h.service
            .configure(id, json!({"receiver_mode":"FM","audio":null}))
            .await
            .unwrap(),
        f["configured"]
    );
    let p = h.service.project(id).await.unwrap();
    let notification = h.service.notify_event(&p, &f["event"]).await.unwrap();
    assert_eq!(notification, f["notification_id"]);
    assert_eq!(
        h.service.notify_event(&p, &f["event"]).await.unwrap(),
        notification
    );
    assert_eq!(h.service.alerts(id, None, 50).await.unwrap(), f["pending"]);
    assert_eq!(h.service.deliver_one().await.unwrap(), f["first_delivery"]);
    // Callback failure text is sanitized to the same network_error wire reason.
    let mut expected = f["failed"].clone();
    expected["items"][0]["delivery"]["last_error"] = json!("HTTP 503");
    assert_eq!(h.service.alerts(id, None, 50).await.unwrap(), expected);
    *h.sources.now.lock().unwrap() = "2026-11-11T13:00:09.123456Z".parse().unwrap();
    assert_eq!(h.service.deliver_one().await.unwrap(), f["early_delivery"]);
    *h.sources.now.lock().unwrap() = "2026-11-11T13:00:10.123456Z".parse().unwrap();
    *h.control.delivery_success.lock().unwrap() = true;
    assert_eq!(h.service.deliver_one().await.unwrap(), f["second_delivery"]);
    assert_eq!(h.service.alerts(id, None, 50).await.unwrap(), f["sent"]);
    *h.sources.now.lock().unwrap() = f["clock"].as_str().unwrap().parse().unwrap();
    assert_eq!(
        h.service
            .acknowledge_alert(id, &notification, json!({}))
            .await
            .unwrap(),
        f["acknowledged"]
    );
    assert_eq!(h.service.alerts(id, None, 50).await.unwrap(), f["alerts"]);
    {
        let deliveries = h.control.deliveries.lock().unwrap();
        assert_eq!(deliveries.len(), 2);
        for delivery in deliveries.iter() {
            assert_eq!(delivery.notification_id, notification);
            assert_eq!(delivery.url, f["transport_requests"][0]["url"]);
            assert_eq!(
                std::str::from_utf8(&delivery.body).unwrap(),
                f["transport_requests"][0]["body"]
            );
            assert_eq!(delivery.secret, "public-synthetic-fixture-key");
        }
    }
    assert!(
        !h.service
            .settings(id)
            .await
            .unwrap()
            .to_string()
            .contains("public-synthetic-fixture-key")
    );
    h.close().await;
}

#[tokio::test]
async fn failed_stop_keeps_control_revoked_and_persists_resume_intent_before_drain() {
    let h = Harness::new().await;
    let id = h.prepared().await;
    h.service
        .update_settings(&id, json!({"autostart":true}))
        .await
        .unwrap();
    let p = h.service.arm(&id, json!({})).await.unwrap();
    let target = ControlTarget {
        session_id: p["active_session_id"].as_str().unwrap().into(),
        stream_id: p["receiver_stream_id"].as_str().unwrap().into(),
    };
    *h.control.fail_stop.lock().unwrap() = true;
    assert!(h.service.close().await.is_err());
    assert_eq!(
        h.service.project(&id).await.unwrap()["resume_requested"],
        true
    );
    assert!(h.service.authorize_control(&target, None).is_err());
    *h.control.fail_stop.lock().unwrap() = false;
    h.service.close().await.unwrap();
    assert_eq!(
        h.service.project(&id).await.unwrap()["resume_requested"],
        true
    );
    h.archive.close().await.unwrap();
}

#[tokio::test]
async fn explicit_disarm_clears_intent_and_never_treats_failed_stop_as_release() {
    let h = Harness::new().await;
    let id = h.prepared().await;
    h.service
        .update_settings(&id, json!({"autostart":true}))
        .await
        .unwrap();
    let p = h.service.arm(&id, json!({})).await.unwrap();
    let target = ControlTarget {
        session_id: p["active_session_id"].as_str().unwrap().into(),
        stream_id: p["receiver_stream_id"].as_str().unwrap().into(),
    };
    *h.control.fail_stop.lock().unwrap() = true;
    assert!(h.service.disarm(&id, json!({})).await.is_err());
    assert!(h.service.authorize_control(&target, None).is_err());
    *h.control.fail_stop.lock().unwrap() = false;
    h.service.disarm(&id, json!({})).await.unwrap();
    assert_eq!(
        h.service.project(&id).await.unwrap()["resume_requested"],
        false
    );
    assert!(h.service.authorize_control(&target, None).is_ok());
    h.close().await;
}

#[tokio::test]
async fn settings_validation_is_atomic_and_does_not_reset_detector_on_rejected_input() {
    let h = Harness::new().await;
    let id = h.prepared().await;
    let before = h.service.project(&id).await.unwrap();
    for patch in [
        json!({"autostart":1}),
        json!({"detector":{"confirmations":true}}),
        json!({"detector":{"confirmations":4,"window_sweeps":3}}),
        json!({"detector":{"margin_db":20,"impulse_db":10}}),
        json!({"detector":{"margin_db":1,"hysteresis_db":2}}),
        json!({"archive":{"minimum_free_bytes":100}}),
        json!({"webhook":{"enabled":true,"url":"https://invalid.example","secret":""}}),
        json!({"webhook":{"enabled":true,"url":"https://user:pass@invalid.example","secret":"synthetic"}}),
        json!({"webhook":{"enabled":true,"url":"https://invalid.example/#fragment","secret":"synthetic"}}),
    ] {
        assert!(h.service.update_settings(&id, patch).await.is_err());
        assert_eq!(h.service.project(&id).await.unwrap(), before);
    }
    h.close().await;
}

#[tokio::test]
async fn audio_requires_explicit_confirmed_matching_input_and_unknown_capability_fails_preflight() {
    let h = Harness::new().await;
    let id = h.prepared().await;
    let before = h.service.project(&id).await.unwrap();
    let unavailable = h.service.audio_inputs().await.unwrap();
    assert_eq!(unavailable["available"], false);
    assert_eq!(unavailable["devices"], json!([]));
    assert!(unavailable["error"].is_string());
    assert!(
        h.service
            .configure(
                &id,
                json!({"audio":{"device":4,"name":"RX input","confirm":true}})
            )
            .await
            .is_err()
    );
    assert_eq!(h.service.project(&id).await.unwrap(), before);
    *h.control.audio_devices.lock().unwrap() = Some(ori_guard::AudioInputs {
        available: Some(true),
        devices: vec![ori_guard::AudioInput {
            index: 4,
            name: "RX é".into(),
            extra: Default::default(),
        }],
        error: None,
    });
    assert!(
        h.service
            .configure(
                &id,
                json!({"audio":{"device":4,"name":"RX é","confirm":true}})
            )
            .await
            .is_err()
    );
    assert!(
        h.service
            .configure(&id, json!({"audio":{"device":4,"name":"RX é","confirm":1}}))
            .await
            .is_err()
    );
    h.service
        .configure(
            &id,
            json!({"audio":{"device":4,"name":"RX é","confirm":true}}),
        )
        .await
        .unwrap();
    assert_eq!(
        h.service.preflight(&id, false).await.unwrap()["ready"],
        true
    );
    h.control
        .audio_devices
        .lock()
        .unwrap()
        .as_mut()
        .unwrap()
        .available = None;
    assert_eq!(
        h.service.preflight(&id, false).await.unwrap()["ready"],
        false
    );
    h.close().await;
}

#[tokio::test]
async fn shared_shutdown_handle_proves_durable_intent_before_blocked_device_drain() {
    let h = Harness::new().await;
    let id = h.prepared().await;
    h.service
        .update_settings(&id, json!({"autostart":true}))
        .await
        .unwrap();
    let armed = h.service.arm(&id, json!({})).await.unwrap();
    let gate = TuneGate::new();
    *h.control.stop_gate.lock().unwrap() = Some(gate.clone());
    assert!(h.service.any_armed());
    let handle = h.service.begin_shutdown().unwrap();
    let same = handle.clone();
    let target = ControlTarget {
        session_id: armed["active_session_id"].as_str().unwrap().into(),
        stream_id: armed["receiver_stream_id"].as_str().unwrap().into(),
    };
    assert!(h.service.authorize_control(&target, None).is_err());
    tokio::time::timeout(std::time::Duration::from_secs(3), handle.intent_persisted())
        .await
        .unwrap()
        .unwrap();
    tokio::time::timeout(std::time::Duration::from_secs(3), gate.entered.notified())
        .await
        .unwrap();
    assert_eq!(
        h.service.project(&id).await.unwrap()["resume_requested"],
        true
    );
    let completed = tokio::spawn(async move { same.completed().await });
    tokio::time::sleep(std::time::Duration::from_millis(25)).await;
    assert!(!completed.is_finished());
    gate.release.notify_one();
    tokio::time::timeout(std::time::Duration::from_secs(3), completed)
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    handle.completed().await.unwrap();
    assert!(!h.service.any_armed());
    h.archive.close().await.unwrap();
}
