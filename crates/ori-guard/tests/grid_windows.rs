mod common;
use common::*;
use serde_json::{Value, json};

fn sample(project: &Value, window: u32, pass: u32, tag: &str) -> Value {
    let mut raw = fixture()["sweeps"][pass as usize % 20].clone();
    raw["id"] = json!(format!("{tag}-{window}-{pass}"));
    raw["session_id"] = project["active_session_id"].clone();
    raw["stream_id"] = project["analyzer_stream_id"].clone();
    let (offset, points) = if window == 0 { (0, 289) } else { (289, 2) };
    raw["frequencies_hz"] = json!(
        (offset..offset + points)
            .map(|n| 1_297_100_000u64 + n * 10_000)
            .collect::<Vec<_>>()
    );
    raw["levels"] = json!(vec![-90.; points as usize]);
    raw["settings"]["rbw_hz"] = json!(30_000);
    raw["settings"]["scan_plan_id"] = project["scan_plan"]["id"].clone();
    raw["settings"]["scan_plan_revision"] = project["scan_plan"]["revision"].clone();
    raw["settings"]["scan_band_id"] = json!("wide");
    raw["settings"]["scan_window_index"] = json!(window);
    raw["settings"]["scan_window_count"] = json!(2);
    raw["settings"]["scan_step_hz"] = json!(10_000);
    raw
}

#[tokio::test]
async fn collect_arm_and_compare_two_windows_of_one_plan_and_reject_mismatched_revision() {
    let h = Harness::new().await;
    let p = h
        .service
        .create(json!({"name":"Grid","site":"RX","session_id":fixture()["session"]["id"]}))
        .await
        .unwrap();
    let id = p["id"].as_str().unwrap();
    let p=h.service.configure(id,json!({"scan_plan":{"name":"RX","bands":[{"id":"wide","name":"Wide",
        "start_hz":1_297_100_000,"stop_hz":1_300_000_000,"points":291,"step_hz":10_000,"rbw_hz":30_000,
        "max_revisit_seconds":15.0}]}})).await.unwrap();
    h.service.save_preparation(id,json!({"antenna_notes":"synthetic","location_confirmed":true,"receive_path_confirmed":true})).await.unwrap();
    let confirm = json!({"name":"Quiet","location":"RX","confirm":true,"band_id":"wide"});
    let quiet = (0..20).map(|pidx| sample(&p, 0, pidx, "ref")).collect();
    h.archive.append_sweeps(quiet).await.unwrap();
    assert!(
        h.service
            .create_baseline(id, confirm.clone())
            .await
            .is_err()
    );
    let progress = h.service.baseline_collection(&p).await.unwrap();
    assert_eq!(progress["bands"][0]["ready_windows"], 1);
    assert_eq!(progress["bands"][0]["sweeps"], 0);
    let mut wrong = sample(&p, 1, 1, "wrong-revision");
    wrong["settings"]["scan_plan_revision"] = json!(3);
    h.archive.append_sweeps(vec![wrong]).await.unwrap();
    assert!(
        h.service
            .create_baseline(id, confirm.clone())
            .await
            .is_err()
    );
    h.archive
        .append_sweeps((0..20).map(|i| sample(&p, 1, i, "ref")).collect())
        .await
        .unwrap();
    let first = h.service.create_baseline(id, confirm).await.unwrap();
    let p = h.service.project(id).await.unwrap();
    let second = h
        .service
        .get_record(
            "baseline",
            p["band_baselines"]["wide:1"].as_str().unwrap(),
            Some(id),
        )
        .await
        .unwrap();
    assert_ne!(first["id"], second["id"]);
    for (index, baseline) in [(0, &first), (1, &second)] {
        assert_eq!(baseline["scan_window_index"], index);
        assert_eq!(baseline["source_refs"].as_array().unwrap().len(), 20);
        for source in baseline["source_refs"].as_array().unwrap() {
            assert!(
                source["sweep_id"]
                    .as_str()
                    .unwrap()
                    .starts_with(&format!("ref-{index}-"))
            );
        }
    }
    assert_eq!(
        h.service.baseline_collection(&p).await.unwrap()["bands"][0]["confirmed_windows"],
        2
    );
    let armed = h.service.arm(id, json!({})).await.unwrap();
    assert_eq!(armed["state"], "armed");
    for window in [0, 1] {
        let mut signal = sample(&armed, window, 1, "live");
        signal["levels"][0] = json!(-40.);
        h.archive.append_sweeps(vec![signal]).await.unwrap();
    }
    assert_eq!(h.service.analyze(id).await.unwrap()["processed_sweeps"], 2);
    for (window, baseline) in [(0, &first), (1, &second)] {
        let result = h
            .service
            .history_comparison(id, &format!("live-{window}-1"))
            .await
            .unwrap();
        assert_eq!(result["baseline_id"], baseline["id"]);
        assert_eq!(result["compatible"], true);
        let next = sample(&armed, window, 2, "next");
        h.archive.append_sweeps(vec![next]).await.unwrap();
        assert_eq!(h.service.analyze(id).await.unwrap()["processed_sweeps"], 1);
    }
    // A later reference for this same plan and geometry cannot retroactively
    // change the reference that was active in an archived armed session.
    let mut later = second.clone();
    later["id"] = json!("later-reference");
    later["created_at"] = json!("2030-01-01T00:00:00Z");
    h.archive.put_monitoring("baseline", later).await.unwrap();
    let old = h.service.history_comparison(id, "live-1-1").await.unwrap();
    assert_eq!(old["baseline_id"], second["id"]);
    let mut wrong = sample(&armed, 1, 3, "invalid");
    wrong["settings"]["scan_plan_revision"] = json!(2);
    h.archive.append_sweeps(vec![wrong]).await.unwrap();
    let cursor = h.service.project(id).await.unwrap()["detector_cursor"].clone();
    assert!(h.service.analyze(id).await.is_err());
    assert_eq!(
        h.service.project(id).await.unwrap()["detector_cursor"],
        cursor
    );
    assert!(
        h.service
            .history_comparison(id, "invalid-1-3")
            .await
            .is_err()
    );
    h.close().await;
}

#[tokio::test]
async fn an_incompatible_last_window_never_publishes_a_partially_confirmed_band() {
    let h = Harness::new().await;
    let p = h
        .service
        .create(json!({"name":"Grid","site":"RX","session_id":fixture()["session"]["id"]}))
        .await
        .unwrap();
    let id = p["id"].as_str().unwrap();
    let p=h.service.configure(id,json!({"scan_plan":{"name":"Survey","bands":[{"id":"wide","name":"Upper",
        "start_hz":1_297_100_000,"stop_hz":1_300_000_000,"points":291,"step_hz":10_000,"rbw_hz":30_000,
        "max_revisit_seconds":15.0}]}})).await.unwrap();
    for window in 0..2 {
        let mut quiet: Vec<_> = (0..20).map(|i| sample(&p, window, i, "quiet")).collect();
        if window == 1 {
            quiet[19]["settings"]["rbw_hz"] = json!(10000);
        }
        h.archive.append_sweeps(quiet).await.unwrap();
    }
    assert_eq!(
        h.service.baseline_collection(&p).await.unwrap()["bands"][0]["ready_windows"],
        2
    );
    assert!(
        h.service
            .create_baseline(
                id,
                json!({"band_id":"wide","name":"Invalid","location":"RX","confirm":true})
            )
            .await
            .is_err()
    );
    let unchanged = h.service.project(id).await.unwrap();
    assert_eq!(unchanged["band_baselines"], json!({}));
    assert_eq!(unchanged["baseline_id"], Value::Null);
    assert!(
        h.archive
            .list_monitoring(
                "baseline",
                ori_archive::MonitoringQuery {
                    project_id: Some(id.into()),
                    ..Default::default()
                }
            )
            .await
            .unwrap()
            .is_empty()
    );
    h.close().await;
}

#[tokio::test]
async fn unreferenced_upper_windows_remain_in_history_while_lower_windows_arm_and_detect() {
    let fixture = fixture();
    let mut session = fixture["session"].clone();
    session["streams"][0]["settings"]["adapter"] = json!("tinysa-serial");
    session["streams"][0]["port"] = json!("COM21");
    session["streams"][1]["settings"]["adapter"] = json!("ardv1-serial");
    session["streams"][1]["port"] = json!("COM22");
    let h = Harness::with_session(session).await;
    let p = h
        .service
        .create(json!({"name":"Boundary","site":"Fixture","session_id":fixture["session"]["id"]}))
        .await
        .unwrap();
    let id = p["id"].as_str().unwrap();
    let p = h.service.configure(id, json!({"scan_plan":{"name":"Boundary","bands":[
        {"id":"high","name":"High","start_hz":900_000_000,"stop_hz":900_010_000,"points":2,"step_hz":10_000,"rbw_hz":10_000,"max_revisit_seconds":15.0},
        {"id":"mixed","name":"Mixed","start_hz":795_670_000,"stop_hz":801_470_000,"points":581,"step_hz":10_000,"rbw_hz":10_000,"max_revisit_seconds":15.0}
    ]}})).await.unwrap();
    h.service.save_preparation(id, json!({"antenna_notes":"Fixture","location_confirmed":true,"receive_path_confirmed":true})).await.unwrap();
    let make_sweep = |project: &Value, band: &str, index: u32, pass: usize, name: &str| {
        let (start, points, windows) = match (band, index) {
            ("high", 0) => (900_000_000, 2, 1),
            ("mixed", 0) => (795_670_000, 290, 3),
            ("mixed", 1) => (798_570_000, 289, 3),
            ("mixed", 2) => (801_460_000, 2, 3),
            _ => unreachable!(),
        };
        let mut raw = fixture["sweeps"][pass].clone();
        raw["id"] = json!(format!("{name}-{band}-{index}-{pass}"));
        raw["session_id"] = project["active_session_id"].clone();
        raw["stream_id"] = project["analyzer_stream_id"].clone();
        raw["frequencies_hz"] = json!((0..points).map(|i| start + i * 10_000).collect::<Vec<_>>());
        raw["levels"] = json!(vec![-85.; points]);
        raw["settings"] = json!({"adapter":"tinysa-serial","simulated":false,
            "device_identity":{"info":"tinySA Ultra HW V0.4.5.1","firmware":"tinySA4_v1.4-235-gea71dfd"},
            "rbw_hz":10_000,"scan_step_hz":10_000,"scan_plan_id":project["scan_plan"]["id"],
            "scan_plan_revision":project["scan_plan"]["revision"],"scan_band_id":band,
            "scan_window_count":windows,"scan_window_index":index});
        raw
    };
    // The first band has enough original passes, including a recognized model
    // and firmware. Neither fact makes those upper-band levels a reference.
    for (band, window) in [("high", 0), ("mixed", 0)] {
        h.archive
            .append_sweeps(
                (0..20)
                    .map(|pass| make_sweep(&p, band, window, pass, "ref"))
                    .collect(),
            )
            .await
            .unwrap();
    }
    let confirm =
        |band| json!({"name":"Reference","location":"Fixture","confirm":true,"band_id":band});
    assert!(
        h.service
            .create_baseline(id, confirm("high"))
            .await
            .is_err()
    );
    let progress = h.service.baseline_collection(&p).await.unwrap();
    assert_eq!(
        progress["bands"][0]["measurement_qualification"]["status"],
        "unverified"
    );
    assert_eq!(progress["bands"][0]["eligible_windows"], 0);
    assert_eq!(progress["bands"][0]["ready"], false);
    assert_eq!(
        progress["bands"][1]["measurement_qualification"]["status"],
        "partial"
    );
    assert_eq!(progress["bands"][1]["eligible_windows"], 1);
    assert_eq!(progress["bands"][1]["excluded_windows"], 2);
    assert_eq!(progress["bands"][1]["ready"], true);
    let baseline = h
        .service
        .create_baseline(id, confirm("mixed"))
        .await
        .unwrap();
    assert_eq!(baseline["scan_window_index"], 0);
    let armed = h.service.arm(id, json!({})).await.unwrap();
    assert_eq!(armed["baseline_id"], baseline["id"]);
    assert!(armed["band_baselines"].get("high:0").is_none());
    assert!(armed["band_baselines"].get("mixed:1").is_none());
    let mut batch = vec![];
    for pass in 0..3 {
        for (band, window) in [("high", 0), ("mixed", 1), ("mixed", 2), ("mixed", 0)] {
            let mut sweep = make_sweep(&armed, band, window, pass, "live");
            sweep["levels"][0] = json!(-35.);
            batch.push(sweep);
        }
    }
    h.archive.append_sweeps(batch).await.unwrap();
    let analyzed = h.service.analyze(id).await.unwrap();
    assert_eq!(analyzed["processed_sweeps"], 3);
    assert_eq!(analyzed["excluded_sweeps"], 9);
    assert_eq!(analyzed["events_updated"], 1);
    let event = h
        .service
        .get_record(
            "event",
            analyzed["event_ids"][0].as_str().unwrap(),
            Some(id),
        )
        .await
        .unwrap();
    assert_eq!(event["center_frequency_hz"], 795_670_000);
    assert_eq!(event["scan_window_index"], 0);
    assert_eq!(h.service.analyze(id).await.unwrap()["processed_sweeps"], 0);
    let original = h
        .service
        .history_original(id, "live-mixed-1-2")
        .await
        .unwrap();
    assert_eq!(original["frequencies_hz"][288], 801_450_000);
    assert_eq!(original["levels"][0], -35.);
    h.close().await;
}

#[tokio::test]
async fn idle_receiver_checks_the_active_grid_window_with_its_own_reference() {
    let h = Harness::new().await;
    let p = h.service.create(json!({"name":"Grid receiver","site":"Fixture","session_id":fixture()["session"]["id"]})).await.unwrap();
    let id = p["id"].as_str().unwrap();
    let p = h.service.configure(id, json!({"scan_plan":{"name":"Survey","bands":[{
        "id":"wide","name":"Upper","start_hz":1_297_100_000,"stop_hz":1_300_000_000,"points":291,
        "step_hz":10_000,"rbw_hz":30_000,"max_revisit_seconds":15.0
    }]}})).await.unwrap();
    h.service.save_preparation(id, json!({"antenna_notes":"Fixture","receive_path_confirmed":true,"location_confirmed":true})).await.unwrap();
    let make = |project: &Value, window, pass, tag| {
        let mut raw = sample(project, window, pass, tag);
        raw["settings"]["adapter"] = json!("tinysa-sim");
        raw["settings"]["simulated"] = json!(true);
        raw
    };
    for window in 0..2 {
        h.archive
            .append_sweeps((0..20).map(|pass| make(&p, window, pass, "ref")).collect())
            .await
            .unwrap();
    }
    h.service
        .create_baseline(
            id,
            json!({"name":"Quiet","location":"Fixture","band_id":"wide","confirm":true}),
        )
        .await
        .unwrap();
    let armed = h.service.arm(id, json!({})).await.unwrap();
    let mut latest = make(&armed, 1, 0, "quiet");
    latest["interval"] =
        json!({"started_at":"2026-11-11T13:00:01Z","ended_at":"2026-11-11T13:00:02Z"});
    h.archive.append_sweeps(vec![latest]).await.unwrap();
    *h.sources.now.lock().unwrap() = "2026-11-11T13:00:03Z".parse().unwrap();
    h.service.tick(id).await.unwrap();
    let checks = &h.service.guard_status(id).await.unwrap()["receiver_checks"];
    assert_eq!(checks["total"], 1);
    assert_eq!(checks["active_band_id"], "wide");
    h.close().await;
}

#[tokio::test]
async fn archived_unverified_reference_cannot_rearm_or_classify_a_crossing_sweep() {
    let f = fixture();
    let mut session = f["session"].clone();
    session["streams"][0]["settings"]["adapter"] = json!("tinysa-serial");
    session["streams"][0]["port"] = json!("COM21");
    session["streams"][1]["settings"]["adapter"] = json!("ardv1-serial");
    session["streams"][1]["port"] = json!("COM22");
    let h = Harness::with_session(session).await;
    let mut project = h
        .service
        .create(
            json!({"name":"Archived reference","site":"Fixture","session_id":f["session"]["id"]}),
        )
        .await
        .unwrap();
    let id = project["id"].as_str().unwrap().to_owned();
    let mut sweeps = f["sweeps"].as_array().unwrap()[..23].to_vec();
    for sweep in &mut sweeps {
        sweep["frequencies_hz"] = json!(
            (0..9)
                .map(|index| 799_950_000 + index * 10_000)
                .collect::<Vec<_>>()
        );
        sweep["settings"] = json!({"adapter":"tinysa-serial","rbw_hz":10_000,
            "measurement_qualification":{"status":"supported"}});
    }
    let source: ori_contracts::Sweep = serde_json::from_value(sweeps[0].clone()).unwrap();
    let mut baseline = f["baseline"].clone();
    baseline["frequencies_hz"] = json!(source.frequencies_hz);
    baseline["fingerprint"] = json!(ori_monitoring::fingerprint(&source).unwrap());
    baseline["project_id"] = json!(id);
    project["baseline_id"] = baseline["id"].clone();
    let mut event = f["first_event"].clone();
    for key in [
        "start_frequency_hz",
        "center_frequency_hz",
        "end_frequency_hz",
    ] {
        event[key] = json!(event[key].as_u64().unwrap() + 699_950_000);
    }
    event["project_id"] = json!(id);
    // Its center lies below 800 MHz, but its original acquisition crossed it.
    assert_eq!(event["center_frequency_hz"], 799_990_000);
    let event_id = event["id"].as_str().unwrap().to_owned();
    let sweep_id = event["source_refs"][0]["sweep_id"]
        .as_str()
        .unwrap()
        .to_owned();
    h.archive.append_sweeps(sweeps).await.unwrap();
    h.archive
        .put_monitoring("baseline", baseline)
        .await
        .unwrap();
    h.archive.put_monitoring("project", project).await.unwrap();
    h.archive.put_monitoring("event", event).await.unwrap();
    h.service.save_preparation(&id,json!({"antenna_notes":"Fixture","receive_path_confirmed":true,"location_confirmed":true})).await.unwrap();
    let comparison = h.service.history_comparison(&id, &sweep_id).await.unwrap();
    assert_eq!(comparison["compatible"], false);
    assert_eq!(comparison["reason"], "reference_required_above_800_mhz");
    assert_eq!(comparison["items"], json!([]));
    let event = h
        .service
        .event_with_recognition(&id, &event_id)
        .await
        .unwrap();
    assert_eq!(event["recognition"]["state"], "unverified");
    assert_eq!(event["recognition"]["hypotheses"], json!([]));
    let page = h.service.recognized_events(&id, "", 1).await.unwrap();
    assert_eq!(page["items"][0]["recognition"]["state"], "unverified");
    assert!(h.service.arm(&id, json!({})).await.is_err());
    assert_eq!(
        h.service.history_original(&id, &sweep_id).await.unwrap()["frequencies_hz"][8],
        800_030_000
    );
    h.close().await;
}
