mod common;
use common::*;
use ori_archive::MonitoringQuery;
use serde_json::{Value, json};

async fn latest_event(h: &Harness, id: &str) -> Value {
    h.archive
        .list_monitoring(
            "event",
            MonitoringQuery {
                project_id: Some(id.into()),
                recent: true,
                limit: 1,
                ..Default::default()
            },
        )
        .await
        .unwrap()
        .remove(0)
}
async fn fresh_peak(h: &Harness, id: &str) -> Value {
    let p = h.service.project(id).await.unwrap();
    let f = fixture();
    let mut sweeps = f["sweeps"].as_array().unwrap()[20..23].to_vec();
    for (index, sweep) in sweeps.iter_mut().enumerate() {
        sweep["id"] = json!(format!("new-run/{index}"));
        sweep["session_id"] = p["active_session_id"].clone();
        sweep["stream_id"] = p["analyzer_stream_id"].clone();
        sweep["interval"] = json!({"started_at":format!("2026-11-11T12:59:{:02}.123456Z",48+index*4),"ended_at":format!("2026-11-11T12:59:{:02}.123456Z",49+index*4)});
    }
    h.archive.append_sweeps(sweeps).await.unwrap();
    h.service.analyze(id).await.unwrap();
    latest_event(h, id).await
}

#[tokio::test]
async fn receiver_queue_is_independent_and_uses_only_post_acknowledgement_original_unit_observations()
 {
    let h = Harness::new().await;
    let id = h.prepared().await;
    let p = h.service.arm(&id, json!({})).await.unwrap();
    let event = fresh_peak(&h, &id).await;
    let sid = p["active_session_id"].as_str().unwrap();
    let receiver = p["receiver_stream_id"].as_str().unwrap();
    let observation = |name: &str, start: &str| json!({"id":name,"session_id":sid,"stream_id":receiver,"interval":{"started_at":start,"ended_at":start},"frequency_hz":100040000,"mode":"AUTO","level":113.0,"level_unit":"s_meter_count","squelch_open":true,"decoded_mode":"DMR","metadata":{"decoder_source":"fixture_wire"}});
    h.archive
        .append_observations(vec![observation("stale", "2026-11-11T12:59:59.123456Z")])
        .await
        .unwrap();
    h.service.tick(&id).await.unwrap();
    tokio::time::timeout(std::time::Duration::from_secs(3), async {
        loop {
            let event = h
                .archive
                .monitoring("event", event["id"].as_str().unwrap())
                .await
                .unwrap()
                .unwrap();
            if !h
                .archive
                .list_investigations(Some(sid), Some("listening"), 1, None)
                .await
                .unwrap()
                .items
                .is_empty()
            {
                break;
            }
            assert_ne!(event["analysis"]["state"], "failed", "{event}");
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    assert!(
        h.control
            .workers
            .lock()
            .unwrap()
            .iter()
            .any(|w| w.stream_id == p["analyzer_stream_id"].as_str().unwrap() && w.alive)
    );
    h.archive
        .append_observations(vec![observation(
            "fresh/%+α",
            "2026-11-11T13:00:00.123457Z",
        )])
        .await
        .unwrap();
    h.sources.advance(11);
    let completed = tokio::time::timeout(std::time::Duration::from_secs(3), async {
        loop {
            let value = h
                .archive
                .monitoring("event", event["id"].as_str().unwrap())
                .await
                .unwrap()
                .unwrap();
            if value["analysis"]["state"] == "observed" {
                break value;
            }
            assert_ne!(value["analysis"]["state"], "failed", "{value}");
            h.sources.advance(1);
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    assert_eq!(
        completed["analysis"]["observation_ids"],
        json!(["fresh/%+α"])
    );
    assert_eq!(completed["analysis"]["receiver_status"][0]["level"], 113.0);
    assert_eq!(
        completed["analysis"]["receiver_status"][0]["level_unit"],
        "s_meter_count"
    );
    assert_eq!(
        completed["analysis"]["classifications"][0]["source"],
        "fixture_wire"
    );
    assert_eq!(
        h.service.guard_status(&id).await.unwrap()["receiver_busy"],
        false
    );
    h.close().await;
}

#[tokio::test]
async fn expired_newest_signal_is_marked_expired_before_considering_old_events() {
    let h = Harness::new().await;
    let id = h.prepared().await;
    h.service.arm(&id, json!({})).await.unwrap();
    let mut event = fresh_peak(&h, &id).await;
    event["last_seen"] = json!("2026-11-11T12:59:00.000000Z");
    h.archive
        .put_monitoring("event", event.clone())
        .await
        .unwrap();
    h.service.tick(&id).await.unwrap();
    let event = h
        .archive
        .monitoring("event", event["id"].as_str().unwrap())
        .await
        .unwrap()
        .unwrap();
    assert_eq!(event["analysis"]["state"], "expired");
    assert!(
        !h.control
            .calls
            .lock()
            .unwrap()
            .iter()
            .any(|x| x.starts_with("tune:"))
    );
    h.close().await;
}

#[tokio::test]
async fn manual_receiver_acquisition_revokes_an_inflight_investigation_epoch() {
    let h = Harness::new().await;
    let id = h.prepared().await;
    let armed = h.service.arm(&id, json!({})).await.unwrap();
    let event = fresh_peak(&h, &id).await;
    h.service.tick(&id).await.unwrap();
    tokio::time::timeout(std::time::Duration::from_secs(3), async {
        loop {
            let e = h
                .archive
                .monitoring("event", event["id"].as_str().unwrap())
                .await
                .unwrap()
                .unwrap();
            if !h
                .archive
                .list_investigations(
                    armed["active_session_id"].as_str(),
                    Some("listening"),
                    1,
                    None,
                )
                .await
                .unwrap()
                .items
                .is_empty()
            {
                break;
            }
            assert_ne!(e["analysis"]["state"], "failed", "{e}");
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    h.service
        .manual_acquire(&id, json!({"role":"receiver"}))
        .await
        .unwrap();
    tokio::time::timeout(std::time::Duration::from_secs(3), async {
        loop {
            let e = h
                .archive
                .monitoring("event", event["id"].as_str().unwrap())
                .await
                .unwrap()
                .unwrap();
            if e["analysis"]["state"] == "interrupted" {
                break;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    h.close().await;
}

#[tokio::test]
async fn session_stop_committed_during_tune_cannot_be_resurrected_by_late_acknowledgement() {
    let h = Harness::new().await;
    let id = h.prepared().await;
    let p = h.service.arm(&id, json!({})).await.unwrap();
    let event = fresh_peak(&h, &id).await;
    let gate = TuneGate::new();
    *h.control.tune_gate.lock().unwrap() = Some(gate.clone());
    h.service.tick(&id).await.unwrap();
    tokio::time::timeout(std::time::Duration::from_secs(3), gate.entered.notified())
        .await
        .unwrap();
    {
        let coordinator = h.archive.domain_coordinator();
        let _transaction =
            tokio::time::timeout(std::time::Duration::from_secs(3), coordinator.lock())
                .await
                .expect("domain coordinator must be released while tune callback waits");
        let sid = p["active_session_id"].as_str().unwrap();
        let session = h.archive.get_session(sid).await.unwrap().unwrap();
        h.archive
            .stop_session(session, fixture()["clock"].as_str().unwrap())
            .await
            .unwrap();
    }
    gate.release.notify_one();
    tokio::time::timeout(std::time::Duration::from_secs(3), async {
        loop {
            let event = h
                .archive
                .monitoring("event", event["id"].as_str().unwrap())
                .await
                .unwrap()
                .unwrap();
            if ["interrupted", "failed"]
                .iter()
                .any(|s| event["analysis"]["state"] == *s)
            {
                break;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    let sid = p["active_session_id"].as_str().unwrap();
    assert_eq!(
        h.archive.get_session(sid).await.unwrap().unwrap()["state"],
        "stopped"
    );
    let investigations = h
        .archive
        .list_investigations(Some(sid), None, 100, None)
        .await
        .unwrap();
    assert_eq!(investigations.items.len(), 1);
    assert_ne!(investigations.items[0]["state"], "listening");
    assert_ne!(investigations.items[0]["state"], "tuning");
    h.close().await;
}

#[tokio::test]
async fn queued_old_permit_does_not_regain_authority_after_manual_acquire_and_return() {
    let h = Harness::new().await;
    let id = h.prepared().await;
    h.service.arm(&id, json!({})).await.unwrap();
    let event = fresh_peak(&h, &id).await;
    let gate = TuneGate::new();
    *h.control.tune_gate.lock().unwrap() = Some(gate.clone());
    h.service.tick(&id).await.unwrap();
    tokio::time::timeout(std::time::Duration::from_secs(3), gate.entered.notified())
        .await
        .unwrap();
    *h.control.tune_gate.lock().unwrap() = None;
    let manual = h
        .service
        .manual_acquire(&id, json!({"role":"receiver"}))
        .await
        .unwrap();
    h.service
        .manual_return(&id, json!({"token":manual["token"]}))
        .await
        .unwrap();
    gate.release.notify_one();
    tokio::time::timeout(std::time::Duration::from_secs(3), async {
        loop {
            let e = h
                .archive
                .monitoring("event", event["id"].as_str().unwrap())
                .await
                .unwrap()
                .unwrap();
            if e["analysis"]["state"] == "interrupted" {
                break;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    assert!(
        !h.control
            .calls
            .lock()
            .unwrap()
            .iter()
            .any(|call| call.ends_with(":100040000:AUTO")),
        "expired queued RF effect must be denied even after a successful manual return"
    );
    h.close().await;
}

#[tokio::test]
async fn cancelled_close_waiter_cannot_detach_an_owned_receiver_job() {
    let h = Harness::new().await;
    let id = h.prepared().await;
    h.service
        .update_settings(&id, json!({"autostart":true}))
        .await
        .unwrap();
    let armed = h.service.arm(&id, json!({})).await.unwrap();
    fresh_peak(&h, &id).await;
    let gate = TuneGate::new();
    *h.control.tune_gate.lock().unwrap() = Some(gate.clone());
    h.service.tick(&id).await.unwrap();
    tokio::time::timeout(std::time::Duration::from_secs(3), gate.entered.notified())
        .await
        .unwrap();
    let first_service = h.service.clone();
    let first = tokio::spawn(async move { first_service.close().await });
    tokio::time::timeout(std::time::Duration::from_secs(3), async {
        loop {
            if h.archive
                .get_session(armed["active_session_id"].as_str().unwrap())
                .await
                .unwrap()
                .unwrap()["state"]
                == "stopped"
            {
                break;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    first.abort();
    let _ = first.await;
    assert_eq!(
        h.service.project(&id).await.unwrap()["resume_requested"],
        true
    );
    let second_service = h.service.clone();
    let second = tokio::spawn(async move { second_service.close().await });
    tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    assert!(
        !second.is_finished(),
        "cleanup retry must still own and drain the blocked job"
    );
    gate.release.notify_one();
    tokio::time::timeout(std::time::Duration::from_secs(3), second)
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    h.archive.close().await.unwrap();
}

fn zero_pcm_wave() -> Vec<u8> {
    let mut b = Vec::with_capacity(48044);
    b.extend_from_slice(b"RIFF");
    b.extend_from_slice(&48036u32.to_le_bytes());
    b.extend_from_slice(b"WAVEfmt ");
    b.extend_from_slice(&16u32.to_le_bytes());
    b.extend_from_slice(&1u16.to_le_bytes());
    b.extend_from_slice(&1u16.to_le_bytes());
    b.extend_from_slice(&24000u32.to_le_bytes());
    b.extend_from_slice(&48000u32.to_le_bytes());
    b.extend_from_slice(&2u16.to_le_bytes());
    b.extend_from_slice(&16u16.to_le_bytes());
    b.extend_from_slice(b"data");
    b.extend_from_slice(&48000u32.to_le_bytes());
    b.resize(48044, 0);
    b
}

#[tokio::test]
async fn explicit_audio_callback_persists_exact_wave_and_scoped_evidence_without_calibration() {
    let h = Harness::new().await;
    let id = h.prepared().await;
    let wave = zero_pcm_wave();
    *h.control.audio_devices.lock().unwrap() = Some(ori_guard::AudioInputs {
        available: Some(true),
        devices: vec![ori_guard::AudioInput {
            index: 4,
            name: "RX fixture".into(),
            extra: Default::default(),
        }],
        error: None,
    });
    *h.control.audio_recording.lock().unwrap() = Some(ori_guard::RecordedAudio {
        wav_bytes: wave.clone(),
        started_at: fixture()["clock"].as_str().unwrap().parse().unwrap(),
        ended_at: "2026-11-11T13:00:01.123456Z".parse().unwrap(),
        sample_rate_hz: 24000,
        channels: 1,
    });
    h.service
        .configure(
            &id,
            json!({"audio":{"device":4,"name":"RX fixture","confirm":true}}),
        )
        .await
        .unwrap();
    h.service
        .update_settings(&id, json!({"detector":{"dwell_seconds":1}}))
        .await
        .unwrap();
    let armed = h.service.arm(&id, json!({})).await.unwrap();
    let event = fresh_peak(&h, &id).await;
    h.service.tick(&id).await.unwrap();
    h.archive.append_observations(vec![json!({"id":"audio-observation","session_id":armed["active_session_id"],"stream_id":armed["receiver_stream_id"],"interval":{"started_at":"2026-11-11T13:00:00.123457Z","ended_at":"2026-11-11T13:00:00.123457Z"},"frequency_hz":100040000,"mode":"AUTO","level":113.0,"level_unit":"s_meter_count","squelch_open":true,"metadata":{}})]).await.unwrap();
    let result = tokio::time::timeout(std::time::Duration::from_secs(3), async {
        loop {
            h.sources.advance(2);
            let value = h
                .archive
                .monitoring("event", event["id"].as_str().unwrap())
                .await
                .unwrap()
                .unwrap();
            if value["analysis"]["state"] == "observed" {
                break value;
            }
            assert_ne!(value["analysis"]["state"], "failed", "{value}");
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    assert!(result["analysis"]["audio_error"].is_null());
    {
        let requests = h.control.audio_requests.lock().unwrap();
        assert_eq!(requests.len(), 1);
        let original = &requests[0].tune_reply;
        assert_eq!(original.applied_tune_revision, 1);
        assert_eq!(original.acknowledged, Some(true));
        assert_eq!(
            original.acknowledged_at.as_str(),
            fixture()["clock"].as_str().unwrap()
        );
        assert_eq!(original.tuned["frequency_hz"], 100040000);
        assert_eq!(original.tuned["mode"], "AUTO");
        assert_eq!(
            original.worker.session_id,
            armed["active_session_id"].as_str().unwrap()
        );
        assert_eq!(
            original.worker.stream_id,
            armed["receiver_stream_id"].as_str().unwrap()
        );
    }

    let artifact = h
        .service
        .event_audio(&id, event["id"].as_str().unwrap())
        .await
        .unwrap();
    assert_eq!(artifact.payload, wave);
    assert_eq!(
        artifact.sha256,
        "e5ea01a23fcb85fd8e608c5c672ea5c22dfab6b0e581d782242f478e4b1c5900"
    );
    assert_eq!(artifact.metadata["sample_rate_hz"], 24000);
    assert_eq!(artifact.metadata["frequency_hz"], 100040000);
    assert_eq!(
        result["analysis"]["receiver_status"][0]["level_unit"],
        "s_meter_count"
    );
    h.close().await;
}

async fn blocked_audio_setup() -> (Harness, String, Value, std::sync::Arc<TuneGate>) {
    let h = Harness::new().await;
    let id = h.prepared().await;
    *h.control.audio_devices.lock().unwrap() = Some(ori_guard::AudioInputs {
        available: Some(true),
        devices: vec![ori_guard::AudioInput {
            index: 4,
            name: "RX fixture".into(),
            extra: Default::default(),
        }],
        error: None,
    });
    *h.control.audio_recording.lock().unwrap() = Some(ori_guard::RecordedAudio {
        wav_bytes: zero_pcm_wave(),
        started_at: fixture()["clock"].as_str().unwrap().parse().unwrap(),
        ended_at: "2026-11-11T13:00:01.123456Z".parse().unwrap(),
        sample_rate_hz: 24000,
        channels: 1,
    });
    h.service
        .configure(
            &id,
            json!({"audio":{"device":4,"name":"RX fixture","confirm":true}}),
        )
        .await
        .unwrap();
    h.service
        .update_settings(&id, json!({"detector":{"dwell_seconds":1}}))
        .await
        .unwrap();
    h.service.arm(&id, json!({})).await.unwrap();
    let event = fresh_peak(&h, &id).await;
    let gate = TuneGate::new();
    *h.control.audio_gate.lock().unwrap() = Some(gate.clone());
    h.service.tick(&id).await.unwrap();
    tokio::time::timeout(std::time::Duration::from_secs(3), gate.entered.notified())
        .await
        .unwrap();
    (h, id, event, gate)
}

#[tokio::test]
async fn audio_request_keeps_original_tune_receipt_after_same_worker_retunes_while_capture_waits() {
    use std::sync::atomic::Ordering;
    let (h, id, event, gate) = blocked_audio_setup().await;
    let request = h.control.audio_requests.lock().unwrap()[0].clone();
    assert_eq!(request.tune_reply.applied_tune_revision, 1);
    assert_eq!(request.tune_reply.tuned["frequency_hz"], 100040000);
    // A new acknowledged tune on the same live worker must invalidate the
    // captured receipt even if policy ownership and requested frequency match.
    h.control.tune_revision.fetch_add(1, Ordering::SeqCst);
    h.service
        .authorize_control(&request.target, Some(&request.permit))
        .unwrap();
    assert!(ori_guard::InstrumentControl::validate_tune(&*h.control, &request.tune_reply).is_err());
    gate.release.notify_one();
    h.sources.advance(2);
    let result = tokio::time::timeout(std::time::Duration::from_secs(3), async {
        loop {
            let value = h
                .archive
                .monitoring("event", event["id"].as_str().unwrap())
                .await
                .unwrap()
                .unwrap();
            if value["analysis"]["state"] == "no_observations" {
                break value;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    assert!(result["analysis"]["audio"].is_null());
    assert!(result["analysis"]["audio_error"].as_str().is_some());
    assert!(
        h.service
            .event_audio(&id, event["id"].as_str().unwrap())
            .await
            .is_err()
    );
    assert_eq!(
        h.control.audio_requests.lock().unwrap()[0]
            .tune_reply
            .applied_tune_revision,
        1
    );
    h.close().await;
}

#[tokio::test]
async fn pending_audio_keeps_original_permit_revoked_after_manual_acquire_and_return() {
    let (h, id, event, gate) = blocked_audio_setup().await;
    let request = h.control.audio_requests.lock().unwrap()[0].clone();
    let old_epoch = request.permit.epoch();
    let manual = h
        .service
        .manual_acquire(&id, json!({"role":"receiver"}))
        .await
        .unwrap();
    h.service
        .manual_return(&id, json!({"token":manual["token"]}))
        .await
        .unwrap();
    assert!(
        h.service
            .authorize_control(&request.target, Some(&request.permit))
            .is_err()
    );
    assert_eq!(request.permit.epoch(), old_epoch);
    assert_eq!(request.tune_reply.applied_tune_revision, 1);
    gate.release.notify_one();
    tokio::time::timeout(std::time::Duration::from_secs(3), async {
        loop {
            let value = h
                .archive
                .monitoring("event", event["id"].as_str().unwrap())
                .await
                .unwrap()
                .unwrap();
            if value["analysis"]["state"] == "interrupted" {
                break;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    assert!(
        h.service
            .event_audio(&id, event["id"].as_str().unwrap())
            .await
            .is_err()
    );
    h.close().await;
}
