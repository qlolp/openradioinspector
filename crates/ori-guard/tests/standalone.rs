mod common;
use common::*;
use serde_json::{Value, json};

fn single(role: &str, settings: Value) -> Value {
    let mut session = fixture()["session"].clone();
    let mut stream = session["streams"]
        .as_array()
        .unwrap()
        .iter()
        .find(|s| s["role"] == role)
        .unwrap()
        .clone();
    stream["settings"] = settings;
    stream["port"] = json!("COM21");
    session["streams"] = json!([stream]);
    session
}

#[tokio::test]
async fn standalone_sources_claim_only_their_real_stream_and_require_evidence() {
    for (role, kind, settings) in [
        (
            "spectrum_analyzer",
            "tinysa",
            json!({"adapter":"tinysa-sim"}),
        ),
        (
            "spectrum_analyzer",
            "rf_explorer",
            json!({"adapter":"rf-explorer-3g-serial","baud":500000}),
        ),
        (
            "receiver",
            "ardv1",
            json!({"adapter":"ardv1-serial","receiver_profile":"ardv1-monitor-v1","mode":"FM","frequency_hz":100000000,"fast_scan":{"start_hz":100000000,"stop_hz":100010000,"step_hz":10000,"settle_s":0.05,"cyclic":true}}),
        ),
    ] {
        let session = single(role, settings.clone());
        let h = Harness::with_session(session.clone()).await;
        let p = h
            .service
            .create(json!({"site":"Lab","name":"One instrument","session_id":session["id"]}))
            .await
            .unwrap();
        let id = p["id"].as_str().unwrap();
        assert_eq!(p["primary_source_kind"], kind);
        assert_eq!(p["primary_stream_id"], session["streams"][0]["id"]);
        assert!(p["secondary_stream_id"].is_null());
        h.service.save_preparation(id,json!({"receive_path_confirmed":true,"location_confirmed":true,"antenna_notes":"Lab"})).await.unwrap();
        assert!(h.service.arm(id, json!({})).await.is_err());
        let prep = h.service.start_preparation(id, json!({})).await.unwrap();
        let sid = prep["project"]["active_session_id"].as_str().unwrap();
        let clone = h.archive.get_session(sid).await.unwrap().unwrap();
        assert_eq!(clone["streams"].as_array().unwrap().len(), 1);
        assert_eq!(
            clone["streams"][0]["settings"]["fast_scan"],
            settings["fast_scan"]
        );
        assert_eq!(prep["workers"].as_array().unwrap().len(), 1);
        let status = h.service.guard_status(id).await.unwrap();
        assert_eq!(status["receiver_checks"]["state"], "unavailable");
        h.service.stop_preparation(id, json!({})).await.unwrap();
        assert!(
            h.service
                .create_baseline(id, json!({"name":"empty","location":"lab","confirm":true}))
                .await
                .is_err()
        );
        h.close().await;
    }
}

#[tokio::test]
async fn upper_tinysa_plan_can_prepare_raw_collection_while_guard_readiness_stays_false() {
    let session = single(
        "spectrum_analyzer",
        json!({"adapter":"tinysa-serial","start_hz":900_000_000,"stop_hz":900_010_000,"points":2,"rbw_hz":10000}),
    );
    let h = Harness::with_session(session.clone()).await;
    let project = h
        .service
        .create(json!({"name":"Upper capture","site":"Lab","session_id":session["id"]}))
        .await
        .unwrap();
    let id = project["id"].as_str().unwrap();
    h.service.configure(id,json!({"scan_plan":{"name":"Upper","bands":[{"id":"high","name":"High","start_hz":900_000_000,"stop_hz":900_010_000,"points":2,"step_hz":10_000,"rbw_hz":10_000,"max_revisit_seconds":15.0}]}})).await.unwrap();
    h.service.save_preparation(id,json!({"antenna_notes":"Fixture","location_confirmed":true,"receive_path_confirmed":true})).await.unwrap();
    let preflight = h.service.preflight(id, false).await.unwrap();
    assert_eq!(preflight["preparation_ready"], true);
    assert_eq!(preflight["ready"], false);
    h.service.start_preparation(id, json!({})).await.unwrap();
    h.service.stop_preparation(id, json!({})).await.unwrap();
    h.close().await;
}

fn stamp(seconds: u64) -> String {
    let start = chrono::DateTime::parse_from_rfc3339("2026-01-01T00:00:00Z").unwrap();
    (start + chrono::Duration::seconds(seconds as i64))
        .to_rfc3339_opts(chrono::SecondsFormat::Micros, true)
}
async fn prepared_single(h: &Harness, session: &Value) -> Value {
    let p = h
        .service
        .create(json!({"name":"Single-source fixture","site":"Bench","session_id":session["id"]}))
        .await
        .unwrap();
    let id = p["id"].as_str().unwrap();
    h.service.save_preparation(id,json!({"antenna_notes":"Recorded test inputs","receive_path_confirmed":true,"location_confirmed":true})).await.unwrap();
    h.service.start_preparation(id, json!({})).await.unwrap()["project"].clone()
}
fn baseline_request() -> Value {
    json!({"name":"Quiet reference","location":"Bench","confirm":true})
}

fn observation(p: &Value, pass: u64, index: u64, count: u32) -> Value {
    let frequency = 100_000_000 + 10_000 * index;
    let plan_key = ori_monitoring::counts::ardv1_plan_key(
        &serde_json::from_value(p["profile"]["receiver"].clone()).unwrap(),
    )
    .unwrap();
    let source = format!(
        "{}-{pass}-{index}",
        p["active_session_id"].as_str().unwrap()
    );
    json!({"id":source,"session_id":p["active_session_id"],"stream_id":p["primary_stream_id"],
        "interval":{"started_at":stamp(4*pass+index),"ended_at":stamp(4*pass+index)},
        "frequency_hz":frequency,"mode":"FM","level":count,"level_unit":"s_meter_count","squelch_open":true,
        "metadata":{"scan_pass":pass,"scan_index":index,"scan_total":2,"scan_start_hz":100_000_000,"scan_stop_hz":100_010_000,
            "scan_step_hz":10000,"scan_segment_index":0,"scan_strategy_requested":"auto","scope_source":"sequential_receiver_readback",
            "receiver_profile":"ardv1-monitor-v1","receiver_firmware":"2504A","adapter":"ardv1-serial","scan_plan_key":plan_key,
            "settle_s":0.015,"requested_frequency_hz":frequency,"observed_frequency_hz":frequency,"requested_mode":"FM","observed_mode":"FM",
            "frequency_readback":true,"frequency_source":"receiver_RX","mode_source":"receiver_RX","simulated":false,
            "raw_receiver_status":format!("RX VFA RF{:04}.{:05} ST010.00 MD0F0 LM{count:03}1",frequency/1_000_000,frequency%1_000_000/10)}})
}

#[tokio::test]
async fn receiver_reference_requires_distinct_verified_passes_then_detects_oriented_counts_with_manual_hold()
 {
    let session = single(
        "receiver",
        json!({"adapter":"ardv1-serial","receiver_profile":"ardv1-monitor-v1","frequency_hz":100_000_000,
        "mode":"FM","fast_scan":{"cyclic":true,"start_hz":100_000_000,"stop_hz":100_010_000,"step_hz":10000}}),
    );
    let h = Harness::with_session(session.clone()).await;
    let p = prepared_single(&h, &session).await;
    let id = p["id"].as_str().unwrap();
    let mut originals = vec![];
    for pass in 1..=20 {
        originals.push(observation(&p, pass, 0, 100));
        if pass < 20 {
            originals.push(observation(&p, pass, 1, 40));
        }
    }
    let mut replay = observation(&p, 19, 1, 40);
    replay["id"] = json!("repeated-original");
    originals.push(replay);
    let mut unverified = observation(&p, 20, 1, 40);
    unverified["metadata"]["frequency_readback"] = json!(false);
    unverified["id"] = json!("unconfirmed-rx");
    originals.push(unverified);
    h.archive.append_observations(originals).await.unwrap();
    let incomplete = h.service.baseline_collection(&p).await.unwrap();
    assert_eq!(incomplete["ready_points"], 1, "{incomplete}");
    assert!(
        h.service
            .create_baseline(id, baseline_request())
            .await
            .is_err()
    );
    h.archive
        .append_observations(vec![observation(&p, 20, 1, 40)])
        .await
        .unwrap();
    let complete = h.service.baseline_collection(&p).await.unwrap();
    assert_eq!(complete["ready"], true, "{complete}");
    let mut unconfirmed = baseline_request();
    unconfirmed["confirm"] = json!(false);
    assert!(h.service.create_baseline(id, unconfirmed).await.is_err());
    assert!(h.service.arm(id, json!({})).await.is_err());
    let baseline = h
        .service
        .create_baseline(id, baseline_request())
        .await
        .unwrap();
    assert_eq!(baseline["level_unit"], "s_meter_count");
    assert!(baseline.get("p95_dbm").is_none());
    let armed = h.service.arm(id, json!({})).await.unwrap();
    let mut captures = vec![];
    for (pass, level) in [(1, 96), (2, 101), (3, 96), (4, 104), (5, 96)] {
        captures.push(observation(&armed, pass, 0, level));
        captures.push(observation(
            &armed,
            pass,
            1,
            match pass {
                2 => 33,
                3 | 4 => 36,
                _ => 40,
            },
        ));
    }
    let later = captures.split_off(4);
    h.archive.append_observations(captures).await.unwrap();
    let first = h.service.analyze(id).await.unwrap();
    assert_eq!(first["events_updated"], 1);
    let impulse_id = first["event_ids"][0].as_str().unwrap();
    let impulse = h
        .archive
        .monitoring("event", impulse_id)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(impulse["kind"], "impulse");
    h.archive.append_observations(later).await.unwrap();
    let detected = h.service.analyze(id).await.unwrap();
    assert_eq!(detected["events_updated"], 2, "{detected}");
    let confirmed = h
        .archive
        .monitoring("event", impulse_id)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(
        confirmed["source_count"], 3,
        "every independent above-threshold pass remains linked"
    );
    assert_eq!(confirmed["kind"], "new_signal");
    assert_eq!(confirmed["impulse_detected"], true);
    let rows = h
        .service
        .list_records("event", Some(id), "", 20, false)
        .await
        .unwrap();
    let sustained = rows["items"]
        .as_array()
        .unwrap()
        .iter()
        .find(|v| v["kind"] == "new_signal" && v["id"] != impulse_id)
        .unwrap();
    assert_eq!(sustained["peak_level"], 96);
    assert_eq!(sustained["source_count"], 3);
    let sources = h
        .service
        .event_sources(id, sustained["id"].as_str().unwrap(), "", 10)
        .await
        .unwrap();
    for s in sources["items"].as_array().unwrap() {
        let obs = h
            .archive
            .get_observation(s["observation_id"].as_str().unwrap())
            .await
            .unwrap()
            .unwrap();
        assert_eq!(obs["frequency_hz"], s["frequency_hz"]);
        assert_eq!(obs["level"], s["level"]);
        assert!(s.get("sweep_id").is_none());
    }
    h.service.tick(id).await.unwrap();
    assert!(
        !h.control
            .calls
            .lock()
            .unwrap()
            .iter()
            .any(|call| call.starts_with("tune:"))
    );
    let lease = h
        .service
        .manual_acquire(id, json!({"role":"receiver"}))
        .await
        .unwrap();
    assert_eq!(h.service.project(id).await.unwrap()["coverage"], "paused");
    h.service
        .manual_tune(
            id,
            json!({"token":lease["token"],"frequency_hz":104_000_000,"mode":"FM"}),
        )
        .await
        .unwrap();
    h.service
        .manual_return(id, json!({"token":lease["token"]}))
        .await
        .unwrap();
    assert_eq!(
        h.service.project(id).await.unwrap()["coverage"],
        "restoring"
    );
    h.archive
        .append_observations(vec![observation(&armed, 6, 0, 100)])
        .await
        .unwrap();
    h.service.tick(id).await.unwrap();
    assert_eq!(h.service.project(id).await.unwrap()["coverage"], "wide");
    assert_eq!(
        h.control
            .calls
            .lock()
            .unwrap()
            .iter()
            .filter(|c| c.starts_with("tune:"))
            .count(),
        1
    );
    assert_eq!(
        h.service.disarm(id, json!({})).await.unwrap()["coverage"],
        "stopped"
    );
    // Manual takeover is a durable operator decision, including an unclean
    // restart with an otherwise valid reference and autostart intent.
    h.service
        .update_settings(id, json!({"autostart":true}))
        .await
        .unwrap();
    h.service.arm(id, json!({})).await.unwrap();
    h.service
        .manual_acquire(id, json!({"role":"receiver"}))
        .await
        .unwrap();
    h.service.close().await.unwrap();
    let reboot = ori_guard::GuardService::with_sources(
        h.archive.clone(),
        h.control.clone(),
        h.sources.clone(),
        h.sources.clone(),
    );
    *h.control.service.lock().unwrap() = std::sync::Arc::downgrade(&reboot);
    reboot.recover_projects().await.unwrap();
    assert_eq!(reboot.guard_status(id).await.unwrap()["active"], false);
    assert!(reboot.arm(id, json!({})).await.is_err());
    let reset = reboot.disarm(id, json!({})).await.unwrap();
    assert!(reset["manual_primary_hold"].is_null());
    assert_eq!(reboot.arm(id, json!({})).await.unwrap()["state"], "armed");
    reboot.disarm(id, json!({})).await.unwrap();
    reboot.close().await.unwrap();
    h.close().await;
}

fn explorer_frame(p: &Value, pass: usize, tile: usize, survey: bool) -> Value {
    let first = 100_000_000 + (tile - 1) * 112 * 10_000;
    let frequencies = (0..112).map(|i| first + i * 10000).collect::<Vec<_>>();
    let sid = p["active_session_id"].as_str().unwrap();
    let mut raw = json!({"id":format!("{sid}-{pass}-{tile}"),"session_id":sid,"stream_id":p["primary_stream_id"],
        "interval":{"started_at":stamp((pass*5+tile) as u64),"ended_at":stamp((pass*5+tile) as u64)},
        "frequencies_hz":frequencies,"levels":vec![-90.;112],"level_unit":"dBm",
        "settings":{"adapter":"rf-explorer-3g-serial","device_mode":"spectrum_analyzer","receive_profile":"rf-explorer-3g-combo-standard-1.34",
            "device_identity":{"main_model":3,"expansion_model":5,"firmware":"01.34"},"active_board":"expansion","active_input":"right",
            "board_min_hz":15_000_000,"board_max_hz":2_700_000_000_u64,"board_max_span_hz":600_000_000,
            "start_hz":first,"stop_hz":first+1110000,"step_hz":10000,"points":112,"rbw_hz":15000,"amplitude_top_dbm":-10,
            "amplitude_bottom_dbm":-120,"offset_db":0,"level_encoding":"dBm = offset_db - u8 / 2","detector":"normal",
            "detector_code":0,"artifact_kind":"raw_spectrum","settings_source":"device_readback","settings_readback":true,
            "timing_source":"host_frame_read","simulated":false}});
    if survey {
        raw["settings"]["survey"] = json!({"start_hz":100_000_000,"stop_hz":102_230_000,"step_hz":10000,
            "tiles":2,"tile":tile,"pass":pass,"pass_id":format!("{sid}-{pass}")});
    }
    raw
}

#[tokio::test]
async fn fixed_explorer_reference_samples_independent_originals_of_the_current_normal_context() {
    let session = single(
        "spectrum_analyzer",
        json!({"adapter":"rf-explorer-3g-serial","baud":500000,"start_hz":100_000_000,"stop_hz":101_110_000}),
    );
    let h = Harness::with_session(session.clone()).await;
    let p = prepared_single(&h, &session).await;
    let id = p["id"].as_str().unwrap();
    let origin = chrono::DateTime::parse_from_rfc3339(&stamp(0)).unwrap();
    let source = |tenths: usize, name: &str, template: &Value| {
        let at = (origin + chrono::Duration::milliseconds(tenths as i64 * 100))
            .to_rfc3339_opts(chrono::SecondsFormat::Micros, true);
        let mut frame = template.clone();
        frame["id"] = json!(name);
        frame["interval"] = json!({"started_at":at,"ended_at":at});
        frame
    };
    let normal = explorer_frame(&p, 0, 1, false);
    let mut held = normal.clone();
    held["settings"]["detector"] = json!("max_hold");
    held["settings"]["detector_code"] = json!(4);
    held["settings"]["artifact_kind"] = json!("processed_trace");
    let mut prior = normal.clone();
    prior["settings"]["rbw_hz"] = json!(30000);
    for (begin, end, frame, label) in [(0, 64, &held, "hold"), (64, 144, &prior, "prior")] {
        let batch = (begin..end)
            .map(|t| source(t, &format!("{label}-{t}"), frame))
            .collect();
        h.archive.append_sweeps(batch).await.unwrap();
    }
    for start in (0..1200).step_by(60) {
        let batch = (start..start + 60)
            .map(|t| source(144 + t, &format!("normal-{t:04}"), &normal))
            .collect();
        h.archive.append_sweeps(batch).await.unwrap();
    }
    let run = h.service.baseline_collection(&p).await.unwrap();
    assert_eq!(run["elapsed_seconds"], 119.9, "{run}");
    assert_eq!(run["ready"], true);
    // Independent originals can share a timestamp after a replay or import.
    // They must not displace a distinct measurement at either sampling boundary.
    for offset in [0, 1199] {
        for start in (0..150).step_by(50) {
            let batch = (start..start + 50)
                .map(|n| source(144 + offset, &format!("copy-{offset}-{n:03}"), &normal))
                .collect();
            h.archive.append_sweeps(batch).await.unwrap();
        }
    }
    let progress = h.service.baseline_collection(&p).await.unwrap();
    assert_eq!(progress["ready"], true, "{progress}");
    assert_eq!(progress["elapsed_seconds"], 119.9);
    let baseline = h
        .service
        .create_baseline(id, baseline_request())
        .await
        .unwrap();
    let refs = baseline["source_refs"].as_array().unwrap();
    assert_eq!(refs.len(), 192);
    assert_eq!(refs.first().unwrap()["sweep_id"], "normal-0000");
    assert_eq!(refs.last().unwrap()["sweep_id"], "normal-1199");
    let ids: Vec<_> = refs
        .iter()
        .map(|r| r["sweep_id"].as_str().unwrap())
        .collect();
    assert!(ids.iter().all(|id| id.starts_with("normal-")));
    assert!(ids.windows(2).all(|pair| pair[0] < pair[1]));
    // A newly observed Normal context starts its own collection.
    prior["settings"]["rbw_hz"] = json!(45000);
    h.archive
        .append_sweeps(vec![source(1400, "new-context", &prior)])
        .await
        .unwrap();
    let changed = h.service.baseline_collection(&p).await.unwrap();
    assert_eq!(changed["ready"], false, "{changed}");
    assert_eq!(changed["sweeps"], 1);
    h.close().await;
}

#[tokio::test]
async fn normal_explorer_window_and_survey_detect_from_original_frames() {
    for survey in [false, true] {
        let mut settings = json!({"adapter":"rf-explorer-3g-serial","baud":500000,"start_hz":100_000_000,"stop_hz":101_110_000});
        if survey {
            settings["survey_step_hz"] = json!(10000);
            settings["stop_hz"] = json!(102_230_000);
        }
        let session = single("spectrum_analyzer", settings);
        let h = Harness::with_session(session.clone()).await;
        let p = prepared_single(&h, &session).await;
        let id = p["id"].as_str().unwrap();
        let tiles = if survey { 2 } else { 1 };
        let mut raw = Vec::new();
        for pass in 1..=20 {
            for tile in 1..=tiles {
                raw.push(explorer_frame(&p, pass, tile, survey));
            }
        }
        let last = raw.pop().unwrap();
        h.archive.append_sweeps(raw).await.unwrap();
        assert!(
            h.service
                .create_baseline(id, baseline_request())
                .await
                .is_err()
        );
        h.archive.append_sweeps(vec![last]).await.unwrap();
        let baseline = h
            .service
            .create_baseline(id, baseline_request())
            .await
            .unwrap();
        assert_eq!(baseline["confirmed"], true);
        let armed = h.service.arm(id, json!({})).await.unwrap();
        let mut current = explorer_frame(&armed, 1, tiles, survey);
        current["levels"][10] = json!(-52.);
        let mut processed = current.clone();
        processed["id"] = json!("display-trace");
        processed["settings"]["artifact_kind"] = json!("processed_trace");
        processed["settings"]["detector"] = json!("max");
        processed["settings"]["detector_code"] = json!(1);
        if survey {
            processed["settings"]["survey"]["pass_id"] = json!("processed");
        }
        h.archive
            .append_sweeps(vec![processed, current.clone()])
            .await
            .unwrap();
        let detection = h.service.analyze(id).await.unwrap();
        assert_eq!(detection["processed_sweeps"], 1, "{detection}");
        assert_eq!(detection["excluded_sweeps"], 1);
        let event = h
            .service
            .event_with_recognition(id, detection["event_ids"][0].as_str().unwrap())
            .await
            .unwrap();
        assert_eq!(event["peak_level"], -52.);
        assert_eq!(event["source_refs"][0]["sweep_id"], current["id"]);
        assert_eq!(event["recognition"]["state"], "new");
        assert_eq!(
            h.service.disarm(id, json!({})).await.unwrap()["coverage"],
            "stopped"
        );
        h.close().await;
    }
}

#[tokio::test]
async fn analyzer_only_guard_arms_and_detects_without_a_receiver() {
    let f = fixture();
    let mut session = f["session"].clone();
    session["streams"]
        .as_array_mut()
        .unwrap()
        .retain(|s| s["role"] == "spectrum_analyzer");
    let h = Harness::with_session(session.clone()).await;
    let p = prepared_single(&h, &session).await;
    let id = p["id"].as_str().unwrap();
    let mut raw = f["sweeps"].as_array().unwrap()[..20].to_vec();
    for s in &mut raw {
        s["session_id"] = p["active_session_id"].clone();
        s["stream_id"] = p["primary_stream_id"].clone();
    }
    h.archive.append_sweeps(raw).await.unwrap();
    h.service
        .create_baseline(id, baseline_request())
        .await
        .unwrap();
    let armed = h.service.arm(id, json!({})).await.unwrap();
    let mut trigger = f["sweeps"][20].clone();
    trigger["session_id"] = armed["active_session_id"].clone();
    trigger["stream_id"] = armed["primary_stream_id"].clone();
    trigger["levels"][0] = json!(-10.);
    h.archive.append_sweeps(vec![trigger]).await.unwrap();
    let result = h.service.analyze(id).await.unwrap();
    assert!(result["events_updated"].as_u64().unwrap() > 0);
    h.service.tick(id).await.unwrap();
    assert!(
        h.service
            .manual_acquire(id, json!({"role":"receiver"}))
            .await
            .is_err()
    );
    assert!(
        !h.control
            .calls
            .lock()
            .unwrap()
            .iter()
            .any(|c| c.starts_with("tune:"))
    );
    h.service.disarm(id, json!({})).await.unwrap();
    h.close().await;
}

#[tokio::test]
async fn duty_reports_the_verified_detector_at_immediate_and_scheduled_start() {
    let session = single(
        "spectrum_analyzer",
        json!({"adapter":"rf-explorer-3g-serial","start_hz":100_000_000,"stop_hz":101_110_000}),
    );
    let h = Harness::with_session(session.clone()).await;
    let p = prepared_single(&h, &session).await;
    let id = p["id"].as_str().unwrap();
    h.archive
        .append_sweeps((1..=20).map(|n| explorer_frame(&p, n, 1, false)).collect())
        .await
        .unwrap();
    h.service
        .create_baseline(id, baseline_request())
        .await
        .unwrap();
    h.service.stop_preparation(id, json!({})).await.unwrap();
    for (weekly, verified) in [(false, true), (true, true), (false, false)] {
        *h.sources.now.lock().unwrap() = "2026-11-23T12:00:00Z".parse().unwrap();
        *h.control.rfe_readback_override.lock().unwrap() = serde_json::from_value(json!({
            "detector":"max_hold","detector_code":4,"artifact_kind":"processed_trace",
            "private_readback":"fixture-private","settings_readback":verified
        }))
        .unwrap();
        let schedule = if weekly {
            json!({"mode":"weekly","timezone":"UTC","weekdays":[1],"start_local":"13:00","end_local":"14:00"})
        } else {
            json!({"mode":"continuous"})
        };
        let mut duty = h
            .service
            .arm_duty(id, json!({"schedule":schedule}))
            .await
            .unwrap();
        if weekly {
            assert_eq!(duty["status"], "waiting");
            *h.sources.now.lock().unwrap() = "2026-11-23T13:00:00Z".parse().unwrap();
            h.service
                .reconcile_duty(id, ori_guard::DutyTrigger::Timer)
                .await
                .unwrap();
            duty = h.service.duty(id).await.unwrap();
        }
        assert_eq!(duty["status"], "preflight_failed");
        let reason = duty["last_error"].as_str().unwrap();
        assert!(
            reason.contains("RF Explorer") && reason.contains("Normal"),
            "{reason}"
        );
        assert_eq!(reason.contains("Max Hold"), verified, "{reason}");
        assert!(!reason.contains("fixture-private"));
        assert_eq!(h.service.duty(id).await.unwrap()["last_error"], reason);
        assert!(!h.service.any_armed());
        h.service.disarm(id, json!({})).await.unwrap();
    }
    *h.control.rfe_readback_override.lock().unwrap() = serde_json::from_value(json!({
        "rbw_hz":30000,"private_readback":"fixture-private"
    }))
    .unwrap();
    let duty = h
        .service
        .arm_duty(id, json!({"schedule":{"mode":"continuous"}}))
        .await
        .unwrap();
    assert_eq!(duty["status"], "preflight_failed");
    assert_eq!(
        duty["last_error"],
        "Проверка прибора, эталона или архива не пройдена"
    );
    h.close().await;
}

#[tokio::test]
async fn explorer_actual_normal_readback_and_calibration_settings_gate_start() {
    let session = single(
        "spectrum_analyzer",
        json!({"adapter":"rf-explorer-3g-serial","start_hz":100_000_000,"stop_hz":101_110_000}),
    );
    let h = Harness::with_session(session.clone()).await;
    let p = prepared_single(&h, &session).await;
    let id = p["id"].as_str().unwrap();
    h.archive
        .append_sweeps((1..=20).map(|n| explorer_frame(&p, n, 1, false)).collect())
        .await
        .unwrap();
    h.service
        .create_baseline(id, baseline_request())
        .await
        .unwrap();
    h.service.stop_preparation(id, json!({})).await.unwrap();
    h.control
        .rfe_readback_override
        .lock()
        .unwrap()
        .insert("rbw_hz".into(), json!(30000));
    assert!(h.service.arm(id, json!({})).await.is_err());
    assert_eq!(h.service.guard_status(id).await.unwrap()["active"], false);
    h.service.disarm(id, json!({})).await.unwrap();
    let max_hold =
        json!({"detector":"max_hold","detector_code":4,"artifact_kind":"processed_trace"});
    let mut no_readback = max_hold.clone();
    no_readback["settings_readback"] = json!(false);
    let mut unverified_profile = max_hold.clone();
    unverified_profile["profile_verified"] = json!(false);
    for (override_fields, verified_max_hold) in [
        (max_hold, true),
        (json!({"detector":"max_hold"}), false),
        (json!({"detector":null,"detector_code":null}), false),
        (no_readback, false),
        (unverified_profile, false),
    ] {
        *h.control.rfe_readback_override.lock().unwrap() =
            serde_json::from_value(override_fields).unwrap();
        for arming in [false, true] {
            let error = if arming {
                h.service.arm(id, json!({})).await.unwrap_err()
            } else {
                h.service
                    .start_preparation(id, json!({}))
                    .await
                    .unwrap_err()
            };
            assert_eq!(error.http_status(), 409);
            assert_eq!(error.api_code(), "monitoring_conflict");
            let message = error.to_string();
            assert!(
                message.contains("RF Explorer") && message.contains("Normal"),
                "{message}"
            );
            assert_eq!(message.contains("Max Hold"), verified_max_hold, "{message}");
            let status = h.service.guard_status(id).await.unwrap();
            assert_eq!(status["active"], false);
            let workers = status["workers"].as_array().unwrap();
            assert_eq!(workers.len(), 1);
            assert_eq!(workers[0]["alive"], false);
            if arming {
                assert_eq!(status["error"], message);
                h.service.disarm(id, json!({})).await.unwrap();
            } else {
                assert_eq!(status["project"]["coverage"], "stopped");
            }
        }
    }
    h.control.rfe_readback_override.lock().unwrap().clear();
    h.service.start_preparation(id, json!({})).await.unwrap();
    h.service.stop_preparation(id, json!({})).await.unwrap();
    h.close().await;
}
