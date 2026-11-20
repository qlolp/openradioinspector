mod common;
use common::*;
use ori_archive::MonitoringQuery;
use ori_guard::receiver_tune_context;
use serde_json::{Value, json};
use std::{sync::atomic::Ordering, time::Duration};

fn receiver_metadata(stream: &str, id: &str, revision: u64) -> Value {
    json!({
        "adapter":"ardv1-sim", "simulated": true, "decoder_source": "fixture_wire",
        "receiver_tune_context": ori_guard::receiver_tune_context(&format!("instance-{stream}"), revision, id),
    })
}

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
    let observation = |name: &str, start: &str| json!({"id":name,"session_id":sid,"stream_id":receiver,"interval":{"started_at":start,"ended_at":start},"frequency_hz":100040000,"mode":"AUTO","level":113.0,"level_unit":"s_meter_count","squelch_open":true,"decoded_mode":"DMR","metadata":receiver_metadata(receiver, name, 1)});
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
        .append_observations({
            let utc = "2026-11-11T13:00:00.123457Z";
            let mut previous = observation("previous-tune", utc);
            previous["metadata"] = receiver_metadata(receiver, "previous-tune", 0);
            let mut replay = observation("replayed", utc);
            replay["metadata"]["replay"] = json!(true);
            vec![
                previous,
                replay,
                observation("future", "2026-11-11T13:00:30.123456Z"),
                observation("fresh/%+α", utc),
            ]
        })
        .await
        .unwrap();
    *h.sources.now.lock().unwrap() = "2026-11-11T13:00:11.123456Z".parse().unwrap();
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
    let (_, job) = h
        .archive
        .get_investigation(completed["analysis"]["investigation_id"].as_str().unwrap())
        .await
        .unwrap()
        .unwrap();
    assert_eq!(job["result_observation_ids"], json!(["fresh/%+α"]));
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
    tokio::time::timeout(std::time::Duration::from_secs(3), async {
        while h.control.audio_requests.lock().unwrap().is_empty() {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    h.archive.append_observations(vec![json!({"id":"audio-observation","session_id":armed["active_session_id"],"stream_id":armed["receiver_stream_id"],"interval":{"started_at":"2026-11-11T13:00:00.123457Z","ended_at":"2026-11-11T13:00:00.123457Z"},"frequency_hz":100040000,"mode":"AUTO","level":113.0,"level_unit":"s_meter_count","squelch_open":true,"metadata":receiver_metadata(armed["receiver_stream_id"].as_str().unwrap(), "audio-observation", 1)})]).await.unwrap();
    *h.sources.now.lock().unwrap() = "2026-11-11T13:00:02.123456Z".parse().unwrap();
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
            assert_eq!(value["analysis"]["state"], "queued", "{value}");
            tokio::time::sleep(Duration::from_millis(10)).await;
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

fn simulated_sweep(
    mut sweep: Value,
    project: &Value,
    band: Option<&str>,
    start_hz: u64,
    id: &str,
) -> Value {
    sweep["id"] = json!(id);
    sweep["session_id"] = project["active_session_id"].clone();
    sweep["stream_id"] = project["analyzer_stream_id"].clone();
    sweep["frequencies_hz"] = json!((0..9).map(|bin| start_hz + 10000 * bin).collect::<Vec<_>>());
    sweep["settings"] =
        json!({"adapter":"tinysa-sim","simulated":true,"rbw_hz":10000,"generator_output":"off"});
    if let Some(band) = band {
        sweep["settings"]["scan_band_id"] = json!(band);
        sweep["settings"]["scan_plan_id"] = project["scan_plan"]["id"].clone();
        sweep["settings"]["scan_plan_revision"] = project["scan_plan"]["revision"].clone();
    }
    sweep
}

async fn arm_with_simulated_sources(h: &Harness, scan_plan: bool) -> (String, Value) {
    let f = fixture();
    let mut p = h
        .service
        .create(json!({
            "name":"Owned receiver fixture",
            "site":"Roof",
            "session_id":f["session"]["id"],
        }))
        .await
        .unwrap();
    let id = p["id"].as_str().unwrap().to_owned();
    if scan_plan {
        p = h.service.configure(&id, json!({"scan_plan":{"name":"Two bands","bands":[
            {"id":"first","name":"First","start_hz":100000000,"stop_hz":100080000,"points":9,"rbw_hz":10000,"max_revisit_seconds":15.0},
            {"id":"next","name":"Next","start_hz":200000000,"stop_hz":200080000,"points":9,"rbw_hz":10000,"max_revisit_seconds":15.0}
        ]}})).await.unwrap();
    }
    h.service.save_preparation(&id,json!({"antenna_notes":"RX fixture","receive_path_confirmed":true,"location_confirmed":true})).await.unwrap();
    let bands = if scan_plan {
        vec![(Some("first"), 100_000_000), (Some("next"), 200_000_000)]
    } else {
        vec![(None, 100_000_000)]
    };
    for (band, start) in bands {
        let sweeps = f["sweeps"].as_array().unwrap()[..20]
            .iter()
            .enumerate()
            .map(|(i, sweep)| {
                simulated_sweep(sweep.clone(), &p, band, start, &format!("{start}/{i}"))
            })
            .collect();
        h.archive.append_sweeps(sweeps).await.unwrap();
        let mut request = json!({"name":"Quiet","location":"Roof","confirm":true});
        if let Some(band) = band {
            request["band_id"] = json!(band);
        }
        h.service.create_baseline(&id, request).await.unwrap();
    }
    h.service
        .update_settings(&id, json!({"detector":{"dwell_seconds":1}}))
        .await
        .unwrap();
    let armed = h.service.arm(&id, json!({})).await.unwrap();
    (id, armed)
}

fn live_sweep(p: &Value, band: Option<&str>, id: &str, second: u32, active: bool) -> Value {
    let start_hz = if band == Some("next") {
        200_000_000
    } else {
        100_000_000
    };
    let mut sweep = simulated_sweep(fixture()["sweeps"][0].clone(), p, band, start_hz, id);
    sweep["interval"] = json!({
        "started_at":format!("2026-11-11T13:00:{second:02}.000000Z"),
        "ended_at":format!("2026-11-11T13:00:{second:02}.900000Z"),
    });
    if active {
        sweep["levels"][4] = json!(-70.0);
    }
    sweep
}

fn set_second(h: &Harness, second: u32) {
    *h.sources.now.lock().unwrap() = format!("2026-11-11T13:00:{second:02}.123456Z")
        .parse()
        .unwrap();
}

fn timed_receiver_sample(
    p: &Value,
    observation_id: &str,
    revision: u64,
    second: u32,
    frequency: u64,
    open: bool,
) -> Value {
    let stream = p["receiver_stream_id"].as_str().unwrap();
    let interval = format!("2026-11-11T13:00:{second:02}.500000Z");
    json!({
        "id":observation_id,
        "session_id":p["active_session_id"],
        "stream_id":stream,
        "interval":{"started_at":interval,"ended_at":interval},
        "frequency_hz":frequency,
        "mode":"AUTO",
        "level":113.0,
        "level_unit":"s_meter_count",
        "squelch_open":open,
        "metadata":{"adapter":"ardv1-sim","simulated":true,
            "receiver_tune_context":receiver_tune_context(&format!("instance-{stream}"), revision, observation_id)},
    })
}

async fn wait_for_tunes(h: &Harness, revision: u64) {
    tokio::time::timeout(Duration::from_secs(3), async {
        while h.control.tune_revision.load(Ordering::SeqCst) < revision {
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    })
    .await
    .unwrap();
}

async fn finish_receiver(h: &Harness, project: &str) -> Value {
    tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            h.sources.advance(1);
            let status = h.service.guard_status(project).await.unwrap();
            if status["receiver_busy"] == false {
                break status;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap()
}

#[tokio::test]
async fn cross_check_requires_the_same_receipt_frequency_and_overlapping_active_sweep() {
    for (revision, second, open, expected, reason) in [
        (
            1,
            5,
            true,
            "observed_both",
            "coincident_receipt_bound_observations",
        ),
        (
            1,
            5,
            false,
            "not_observed",
            "receiver_squelch_closed_during_tinysa_detection",
        ),
        (0, 5, true, "inconclusive", "missing_receiver_readback"),
        (1, 6, true, "inconclusive", "no_coincident_tinysa_source"),
    ] {
        let h = Harness::new().await;
        let (id, armed) = arm_with_simulated_sources(&h, false).await;
        set_second(&h, 4);
        h.archive
            .append_sweeps(vec![live_sweep(&armed, None, "initial", 1, true)])
            .await
            .unwrap();
        h.service.tick(&id).await.unwrap();
        wait_for_tunes(&h, 1).await;
        set_second(&h, 7);
        h.archive
            .append_sweeps(vec![live_sweep(&armed, None, "matched", 5, true)])
            .await
            .unwrap();
        h.service.analyze(&id).await.unwrap();
        h.archive
            .append_observations(vec![timed_receiver_sample(
                &armed,
                "readback",
                revision,
                second,
                100_040_000,
                open,
            )])
            .await
            .unwrap();
        finish_receiver(&h, &id).await;
        let event = latest_event(&h, &id).await;
        let analysis = &event["analysis"];
        assert_eq!(analysis["verification"], expected, "{event}");
        assert_eq!(
            analysis["verification_evidence"]["reason"], reason,
            "{event}"
        );
        assert!(analysis["event_age_seconds"].as_f64().unwrap() > 2.);
        if expected != "inconclusive" {
            let evidence = &analysis["verification_evidence"];
            assert_eq!(evidence["simulated"], true);
            assert_eq!(evidence["analyzer"]["sweep_id"], "matched");
            assert_eq!(evidence["analyzer"]["level"], -70.0);
            assert_eq!(evidence["analyzer"]["level_unit"], "dBm");
            assert_eq!(evidence["receiver"]["observation_id"], "readback");
            assert_eq!(evidence["receiver"]["level"], 113.0);
            assert_eq!(evidence["receiver"]["level_unit"], "s_meter_count");
        }
        h.close().await;
    }
}

#[tokio::test]
async fn control_check_waits_for_a_slow_committed_sweep_and_preserves_original_units() {
    let h = Harness::new().await;
    let (id, p) = arm_with_simulated_sources(&h, false).await;
    h.service.tick(&id).await.unwrap();
    assert_eq!(
        h.service.guard_status(&id).await.unwrap()["receiver_checks"]["state"],
        "waiting"
    );
    assert_eq!(h.control.tune_revision.load(Ordering::SeqCst), 0);
    set_second(&h, 4);
    let mut historical = live_sweep(&p, None, "outside-active-run", 1, false);
    historical["interval"] =
        json!({"started_at":"2026-11-11T12:59:58Z","ended_at":"2026-11-11T12:59:59Z"});
    h.archive.append_sweeps(vec![historical]).await.unwrap();
    h.service.tick(&id).await.unwrap();
    assert_eq!(h.control.tune_revision.load(Ordering::SeqCst), 0);
    h.archive
        .append_sweeps(vec![live_sweep(&p, None, "live-control-source", 2, false)])
        .await
        .unwrap();
    h.service.tick(&id).await.unwrap();
    wait_for_tunes(&h, 1).await;
    assert_eq!(
        h.service.guard_status(&id).await.unwrap()["receiver_checks"]["state"],
        "checking"
    );
    tokio::time::sleep(Duration::from_millis(65)).await;
    h.sources.advance(3);
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert_eq!(
        h.service.guard_status(&id).await.unwrap()["receiver_checks"]["state"],
        "checking",
        "the baseline records a four-second sweep period"
    );
    h.archive
        .append_sweeps(vec![live_sweep(
            &p,
            None,
            "coincident-control-source",
            7,
            false,
        )])
        .await
        .unwrap();
    h.archive
        .append_observations(vec![
            timed_receiver_sample(&p, "live-control-readback", 1, 7, 100_040_000, false),
            timed_receiver_sample(&p, "later-without-sweep", 1, 8, 100_040_000, false),
        ])
        .await
        .unwrap();
    set_second(&h, 9);
    let status = finish_receiver(&h, &id).await;
    let checks = &status["receiver_checks"];
    assert_eq!(checks["state"], "complete", "{checks}");
    assert_eq!(checks["completed"], 1);
    assert_eq!(checks["total"], 1);
    let check = &checks["items"][0];
    assert_eq!(check["state"], "sampled", "{check}");
    assert_eq!(
        check["reason"],
        "coincident_quiet_analyzer_and_receiver_observations"
    );
    assert_eq!(check["scan_band_id"], Value::Null);
    assert_eq!(check["analyzer_anchor"]["sweep_id"], "live-control-source");
    assert_eq!(check["analyzer"]["sweep_id"], "coincident-control-source");
    assert_eq!(check["analyzer"]["level_unit"], "dBm");
    assert_eq!(check["receiver"]["observation_id"], "live-control-readback");
    assert_eq!(check["receiver"]["level_unit"], "s_meter_count");
    h.service.tick(&id).await.unwrap();
    assert_eq!(h.control.tune_revision.load(Ordering::SeqCst), 1);
    h.service.disarm(&id, json!({})).await.unwrap();
    let stopped = h.service.guard_status(&id).await.unwrap();
    assert_eq!(stopped["receiver_checks"]["state"], "inactive");
    assert_eq!(stopped["receiver_checks"]["items"][0], *check);
    h.close().await;
}

#[tokio::test]
async fn idle_readback_with_only_a_noisy_coincident_sweep_is_inconclusive() {
    let h = Harness::new().await;
    let (id, p) = arm_with_simulated_sources(&h, false).await;
    set_second(&h, 4);
    h.archive
        .append_sweeps(vec![live_sweep(&p, None, "quiet-anchor", 1, false)])
        .await
        .unwrap();
    h.service.tick(&id).await.unwrap();
    wait_for_tunes(&h, 1).await;
    h.archive
        .append_sweeps(vec![
            live_sweep(&p, None, "coincident-but-noisy", 5, true),
            live_sweep(&p, None, "quiet-but-too-late", 6, false),
        ])
        .await
        .unwrap();
    h.archive
        .append_observations(vec![timed_receiver_sample(
            &p,
            "uncorroborated-control",
            1,
            5,
            100_040_000,
            true,
        )])
        .await
        .unwrap();
    set_second(&h, 8);
    let done = finish_receiver(&h, &id).await;
    let check = &done["receiver_checks"]["items"][0];
    assert_eq!(done["receiver_checks"]["completed"], 1);
    assert_eq!(check["state"], "inconclusive");
    assert_eq!(check["reason"], "no_coincident_tinysa_source");
    assert_eq!(check["analyzer_anchor"]["sweep_id"], "quiet-anchor");
    assert!(check["analyzer"].is_null(), "{check}");
    assert_eq!(
        check["receiver"]["observation_id"],
        "uncorroborated-control"
    );
    h.close().await;
}

#[tokio::test]
async fn waiting_event_precedes_the_next_band_control_point_and_checks_never_repeat() {
    let h = Harness::new().await;
    let (id, p) = arm_with_simulated_sources(&h, true).await;
    set_second(&h, 4);
    h.archive
        .append_sweeps(vec![
            live_sweep(&p, Some("first"), "first-quiet", 1, false),
            live_sweep(&p, Some("next"), "next-quiet", 1, false),
        ])
        .await
        .unwrap();
    h.service.tick(&id).await.unwrap();
    wait_for_tunes(&h, 1).await;
    assert_eq!(
        h.service.guard_status(&id).await.unwrap()["receiver_checks"]["active_band_id"],
        "first"
    );
    h.archive
        .append_sweeps(vec![live_sweep(
            &p,
            Some("first"),
            "priority-event",
            2,
            true,
        )])
        .await
        .unwrap();
    h.service.tick(&id).await.unwrap();
    let event = latest_event(&h, &id).await;
    assert!(event["analysis"].is_null(), "{event}");
    assert_eq!(h.control.tune_revision.load(Ordering::SeqCst), 1);
    let first_done = finish_receiver(&h, &id).await;
    let attempts = (0..20)
        .map(|i| {
            let mut record = first_done["receiver_checks"]["items"][0].clone();
            record["id"] = json!(format!("next-interrupted-{i}"));
            record["state"] = json!("interrupted");
            record["scan_band_id"] = json!("next");
            record["baseline_id"] = p["band_baselines"]["next"].clone();
            ori_archive::Mutation::Monitoring {
                kind: "receiver-check".into(),
                value: record,
            }
        })
        .collect();
    h.archive.apply(attempts).await.unwrap();
    let checks = h.service.guard_status(&id).await.unwrap()["receiver_checks"].clone();
    assert_eq!(checks["completed"], 1, "{checks}");
    assert_eq!(checks["items"].as_array().unwrap().len(), 2);
    assert_eq!(checks["items"][0]["id"], "next-interrupted-19");
    h.service.tick(&id).await.unwrap();
    wait_for_tunes(&h, 2).await;
    finish_receiver(&h, &id).await;
    h.service.tick(&id).await.unwrap();
    wait_for_tunes(&h, 3).await;
    let finished = finish_receiver(&h, &id).await;
    assert_eq!(finished["receiver_checks"]["completed"], 2, "{finished}");
    assert_eq!(finished["receiver_checks"]["state"], "complete");
    let calls = h
        .control
        .calls
        .lock()
        .unwrap()
        .iter()
        .filter(|call| call.starts_with("tune:"))
        .cloned()
        .collect::<Vec<_>>();
    assert_eq!(calls.len(), 3);
    assert!(calls[0].ends_with(":100040000:AUTO"), "{calls:?}");
    assert!(calls[1].ends_with(":100040000:AUTO"), "{calls:?}");
    assert!(calls[2].ends_with(":200040000:AUTO"), "{calls:?}");
    h.service.tick(&id).await.unwrap();
    assert_eq!(h.control.tune_revision.load(Ordering::SeqCst), 3);
    h.close().await;
}

#[tokio::test]
async fn manual_receiver_handoff_revokes_queued_idle_tune_and_retries_with_fresh_evidence() {
    let h = Harness::new().await;
    let (id, p) = arm_with_simulated_sources(&h, false).await;
    set_second(&h, 4);
    h.archive
        .append_sweeps(vec![live_sweep(&p, None, "idle-to-revoke", 1, false)])
        .await
        .unwrap();
    let gate = TuneGate::new();
    *h.control.tune_gate.lock().unwrap() = Some(gate.clone());
    h.service.tick(&id).await.unwrap();
    tokio::time::timeout(Duration::from_secs(3), gate.entered.notified())
        .await
        .unwrap();
    let lease = h
        .service
        .manual_acquire(&id, json!({"role":"receiver"}))
        .await
        .unwrap();
    gate.release.notify_one();
    let status = finish_receiver(&h, &id).await;
    assert_eq!(status["receiver_checks"]["state"], "paused");
    assert_eq!(status["receiver_checks"]["completed"], 0);
    let attempt = &status["receiver_checks"]["items"][0];
    assert_eq!(attempt["state"], "interrupted");
    assert_eq!(attempt["reason"], "control_context_revoked");
    assert!(attempt["receiver"].is_null());
    assert_eq!(h.control.tune_revision.load(Ordering::SeqCst), 0);
    assert!(
        h.control
            .calls
            .lock()
            .unwrap()
            .iter()
            .all(|call| !call.starts_with("tune:"))
    );
    let interrupted_id = attempt["id"].as_str().unwrap();
    *h.control.tune_gate.lock().unwrap() = None;
    set_second(&h, 6);
    h.archive
        .append_sweeps(vec![live_sweep(&p, None, "during-manual-hold", 5, false)])
        .await
        .unwrap();
    h.service.tick(&id).await.unwrap();
    set_second(&h, 7);
    h.service
        .manual_return(&id, json!({"token":lease["token"]}))
        .await
        .unwrap();
    h.service.tick(&id).await.unwrap();
    let awaiting = h.service.guard_status(&id).await.unwrap();
    assert_eq!(awaiting["receiver_checks"]["state"], "waiting");
    assert_eq!(awaiting["receiver_checks"]["completed"], 0);
    assert_eq!(
        h.control.tune_revision.load(Ordering::SeqCst),
        1,
        "only the manual return retune"
    );
    set_second(&h, 10);
    h.archive
        .append_sweeps(vec![live_sweep(&p, None, "new-after-handoff", 8, false)])
        .await
        .unwrap();
    h.service.tick(&id).await.unwrap();
    wait_for_tunes(&h, 2).await;
    h.archive
        .append_sweeps(vec![live_sweep(
            &p,
            None,
            "coincident-after-handoff",
            11,
            false,
        )])
        .await
        .unwrap();
    h.archive
        .append_observations(vec![timed_receiver_sample(
            &p,
            "readback-after-handoff",
            2,
            11,
            100_040_000,
            false,
        )])
        .await
        .unwrap();
    set_second(&h, 13);
    let finished = finish_receiver(&h, &id).await;
    assert_eq!(finished["receiver_checks"]["completed"], 1);
    let items = finished["receiver_checks"]["items"].as_array().unwrap();
    assert_eq!(items.len(), 1, "{items:?}");
    assert_eq!(items[0]["state"], "sampled");
    assert_eq!(items[0]["analyzer_anchor"]["sweep_id"], "new-after-handoff");
    assert_eq!(items[0]["analyzer"]["sweep_id"], "coincident-after-handoff");
    let archived = h
        .archive
        .monitoring("receiver-check", interrupted_id)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(archived["state"], "interrupted");
    h.close().await;
}

#[tokio::test]
async fn narrow_search_revokes_queued_idle_tune_with_unchanged_receiver_epoch() {
    let h = Harness::new().await;
    let (id, p) = arm_with_simulated_sources(&h, false).await;
    set_second(&h, 4);
    h.archive
        .append_sweeps(vec![live_sweep(&p, None, "pre-search-anchor", 1, false)])
        .await
        .unwrap();
    let gate = TuneGate::new();
    *h.control.tune_gate.lock().unwrap() = Some(gate.clone());
    h.service.tick(&id).await.unwrap();
    tokio::time::timeout(Duration::from_secs(3), gate.entered.notified())
        .await
        .unwrap();
    let mut event = fixture()["first_event"].clone();
    event["id"] = json!("idle-check-search-event");
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
            json!({
                "token":lease["token"],"event_id":"idle-check-search-event","span_hz":20000,
            }),
        )
        .await
        .unwrap();
    assert_eq!(search["coverage"], "narrow");
    gate.release.notify_one();
    let status = finish_receiver(&h, &id).await;
    assert_eq!(status["receiver_checks"]["state"], "paused");
    assert_eq!(status["receiver_checks"]["completed"], 0);
    assert_eq!(
        status["receiver_checks"]["items"][0]["state"],
        "interrupted"
    );
    assert_eq!(h.control.tune_revision.load(Ordering::SeqCst), 0);
    *h.control.tune_gate.lock().unwrap() = None;
    set_second(&h, 6);
    let mut restoring = live_sweep(&p, None, "restore-wide-analyzer", 6, false);
    restoring["interval"]["started_at"] = json!("2026-11-11T13:00:06.123456Z");
    *h.control.sweep_on_start.lock().unwrap() = Some((h.archive.clone(), restoring));
    h.service
        .manual_return(&id, json!({"token":lease["token"]}))
        .await
        .unwrap();
    tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            let status = h.service.guard_status(&id).await.unwrap();
            if status["project"]["coverage"] == "wide" {
                break;
            }
            assert_ne!(status["project"]["state"], "error", "{status}");
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    })
    .await
    .unwrap();
    set_second(&h, 8);
    h.service.tick(&id).await.unwrap();
    assert_eq!(h.control.tune_revision.load(Ordering::SeqCst), 0);
    h.archive
        .append_sweeps(vec![live_sweep(&p, None, "fresh-wide-analyzer", 9, false)])
        .await
        .unwrap();
    set_second(&h, 10);
    h.service.tick(&id).await.unwrap();
    wait_for_tunes(&h, 1).await;
    finish_receiver(&h, &id).await;
    h.close().await;
}

#[tokio::test]
async fn canonical_session_stop_revokes_queued_idle_tune_and_discards_evidence() {
    let h = Harness::new().await;
    let (id, p) = arm_with_simulated_sources(&h, false).await;
    set_second(&h, 4);
    h.archive
        .append_sweeps(vec![live_sweep(&p, None, "pre-stop-anchor", 1, false)])
        .await
        .unwrap();
    let gate = TuneGate::new();
    *h.control.tune_gate.lock().unwrap() = Some(gate.clone());
    h.service.tick(&id).await.unwrap();
    tokio::time::timeout(Duration::from_secs(3), gate.entered.notified())
        .await
        .unwrap();
    let session = h
        .archive
        .get_session(p["active_session_id"].as_str().unwrap())
        .await
        .unwrap()
        .unwrap();
    h.archive
        .stop_session(session, "2026-11-11T13:00:05.000000Z")
        .await
        .unwrap();
    gate.release.notify_one();
    let status = finish_receiver(&h, &id).await;
    let check = &status["receiver_checks"]["items"][0];
    assert_eq!(check["state"], "interrupted");
    assert!(check["receiver"].is_null());
    assert!(check["analyzer"].is_null());
    assert_eq!(h.control.tune_revision.load(Ordering::SeqCst), 0);
    h.close().await;
}
