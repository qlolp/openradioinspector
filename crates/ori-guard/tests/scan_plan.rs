mod common;
use common::*;
use ori_archive::MonitoringQuery;
use serde_json::{Value, json};

fn proposal() -> Value {
    json!({"name":"Local survey", "bands":[
        {"id":"fm","name":"FM","start_hz":88000000,"stop_hz":108000000,"points":201,"rbw_hz":10000,"max_revisit_seconds":15.0},
        {"id":"uhf","name":"UHF","start_hz":430000000,"stop_hz":440000000,"points":101,"rbw_hz":30000,"max_revisit_seconds":15.0}
    ]})
}

#[tokio::test]
async fn saved_scan_plan_is_versioned_durable_and_does_not_touch_instruments() {
    let h = Harness::new().await;
    let p = h
        .service
        .create(json!({"name":"Survey","site":"Room","session_id":fixture()["session"]["id"]}))
        .await
        .unwrap();
    let id = p["id"].as_str().unwrap();
    let saved = h
        .service
        .configure(id, json!({"scan_plan":proposal()}))
        .await
        .unwrap();
    assert_eq!(saved["scan_plan"]["revision"], 1);
    assert_eq!(
        saved["scan_plan"]["config"]["bands"][1]["start_hz"],
        430000000
    );
    assert_eq!(saved["baseline_id"], Value::Null);
    let prior = saved["scan_plan"]["id"].as_str().unwrap();
    let durable = h
        .archive
        .monitoring("scan-plan", prior)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(durable, saved["scan_plan"]);
    let mut changed = proposal();
    changed["bands"][0]["stop_hz"] = json!(107000000);
    let revised = h
        .service
        .configure(id, json!({"scan_plan":changed}))
        .await
        .unwrap();
    assert_eq!(revised["scan_plan"]["revision"], 2);
    assert_ne!(revised["scan_plan"]["id"], saved["scan_plan"]["id"]);
    assert_eq!(
        h.archive
            .monitoring("scan-plan", prior)
            .await
            .unwrap()
            .unwrap(),
        durable
    );
    assert!(h.control.calls.lock().unwrap().is_empty());
    h.close().await;
}

#[tokio::test]
async fn invalid_scan_plans_are_rejected_atomically_without_downgrading_profile() {
    let h = Harness::new().await;
    let p = h
        .service
        .create(json!({"name":"Survey","site":"Room","session_id":fixture()["session"]["id"]}))
        .await
        .unwrap();
    let id = p["id"].as_str().unwrap();
    let mut duplicate = proposal();
    duplicate["bands"][1]["id"] = json!("fm");
    let mut inverted = proposal();
    inverted["bands"][0]["stop_hz"] = json!(88000000);
    let mut bad_rbw = proposal();
    bad_rbw["bands"][0]["rbw_hz"] = json!(0);
    let mut injected = proposal();
    injected["bands"][0]["output"] = json!(true);
    let mut too_many = proposal();
    too_many["bands"] = json!(
        (0..17)
            .map(|i| {
                let mut b = proposal()["bands"][0].clone();
                b["id"] = json!(format!("band{i}"));
                b
            })
            .collect::<Vec<_>>()
    );
    for config in [
        json!({"name":"Empty","bands":[]}),
        duplicate,
        inverted,
        bad_rbw,
        injected,
        too_many,
    ] {
        assert!(
            h.service
                .configure(id, json!({"scan_plan":config}))
                .await
                .is_err()
        );
        assert_eq!(h.service.project(id).await.unwrap(), p);
    }
    assert!(
        h.archive
            .list_monitoring(
                "scan-plan",
                MonitoringQuery {
                    project_id: Some(id.into()),
                    ..Default::default()
                }
            )
            .await
            .unwrap()
            .is_empty()
    );
    assert!(h.control.calls.lock().unwrap().is_empty());
    h.close().await;
}

#[tokio::test]
async fn each_band_requires_its_own_twenty_compatible_sweeps_and_confirmation() {
    let h = Harness::new().await;
    let p = h
        .service
        .create(json!({"name":"Survey","site":"Room","session_id":fixture()["session"]["id"]}))
        .await
        .unwrap();
    let id = p["id"].as_str().unwrap();
    let mut config = proposal();
    for (i, start) in [100000000u64, 200000000].into_iter().enumerate() {
        config["bands"][i]["start_hz"] = json!(start);
        config["bands"][i]["stop_hz"] = json!(start + 80000);
        config["bands"][i]["points"] = json!(9);
    }
    let p = h
        .service
        .configure(id, json!({"scan_plan":config}))
        .await
        .unwrap();
    let plan = p["scan_plan"]["id"].as_str().unwrap();
    let f = fixture();
    let mut sources = vec![];
    for (band, offset) in [("fm", 0u64), ("uhf", 100000000)] {
        for (index, sweep) in f["sweeps"].as_array().unwrap()[..20].iter().enumerate() {
            let mut sweep = sweep.clone();
            sweep["id"] = json!(format!("{band}-{index:02}"));
            if offset > 0 {
                sweep["frequencies_hz"] = json!([
                    200000000u64,
                    200010000,
                    200020000,
                    200030000,
                    200040000,
                    200050000,
                    200060000,
                    200070000,
                    200080000
                ]);
            }
            sweep["settings"]["scan_band_id"] = json!(band);
            sweep["settings"]["scan_plan_id"] = json!(plan);
            sources.push(sweep);
        }
    }
    h.archive
        .append_sweeps(sources[..20].to_vec())
        .await
        .unwrap();
    assert!(
        h.service
            .create_baseline(
                id,
                json!({"name":"B","location":"Room","confirm":true,"band_id":"uhf"})
            )
            .await
            .is_err()
    );
    let a = h
        .service
        .create_baseline(
            id,
            json!({"name":"A","location":"Room","confirm":true,"band_id":"fm"}),
        )
        .await
        .unwrap();
    assert_eq!(a["frequencies_hz"][0], 100000000);
    assert_eq!(a["scan_band_id"], "fm");
    assert_ne!(h.service.project(id).await.unwrap()["state"], "ready");
    h.archive
        .append_sweeps(sources[20..].to_vec())
        .await
        .unwrap();
    let b = h
        .service
        .create_baseline(
            id,
            json!({"name":"B","location":"Room","confirm":true,"band_id":"uhf"}),
        )
        .await
        .unwrap();
    assert_eq!(b["frequencies_hz"][0], 200000000);
    assert_eq!(b["scan_band_id"], "uhf");
    let p = h.service.project(id).await.unwrap();
    assert_eq!(p["state"], "ready");
    assert_eq!(p["band_baselines"]["fm"], a["id"]);
    assert_eq!(p["band_baselines"]["uhf"], b["id"]);
    assert!(
        h.service
            .create_baseline(
                id,
                json!({"name":"Unscoped","location":"Room","confirm":true})
            )
            .await
            .is_err()
    );
    h.close().await;
}

#[tokio::test]
async fn interleaved_bands_keep_separate_detectors_events_and_history_references() {
    let h = Harness::new().await;
    let p = h
        .service
        .create(json!({"name":"Survey","site":"Room","session_id":fixture()["session"]["id"]}))
        .await
        .unwrap();
    let id = p["id"].as_str().unwrap();
    let mut config = proposal();
    for (i, start) in [100000000u64, 200000000].into_iter().enumerate() {
        config["bands"][i]["start_hz"] = json!(start);
        config["bands"][i]["stop_hz"] = json!(start + 80000);
        config["bands"][i]["points"] = json!(9);
    }
    let p = h
        .service
        .configure(id, json!({"scan_plan":config}))
        .await
        .unwrap();
    let plan = p["scan_plan"]["id"].clone();
    let f = fixture();
    let mut quiet = vec![];
    for (band, frequencies) in [
        (
            "fm",
            json!([
                100000000u64,
                100010000,
                100020000,
                100030000,
                100040000,
                100050000,
                100060000,
                100070000,
                100080000
            ]),
        ),
        (
            "uhf",
            json!([
                200000000u64,
                200010000,
                200020000,
                200030000,
                200040000,
                200050000,
                200060000,
                200070000,
                200080000
            ]),
        ),
    ] {
        for index in 0..20 {
            let mut sweep = f["sweeps"][index].clone();
            sweep["id"] = json!(format!("quiet-{band}-{index:02}"));
            sweep["frequencies_hz"] = frequencies.clone();
            sweep["levels"] = json!([-90., -90., -90., -90., -90., -90., -90., -90., -90.]);
            sweep["settings"]["scan_band_id"] = json!(band);
            sweep["settings"]["scan_plan_id"] = plan.clone();
            quiet.push(sweep);
        }
    }
    h.archive.append_sweeps(quiet.clone()).await.unwrap();
    let a = h
        .service
        .create_baseline(
            id,
            json!({"name":"A","location":"Room","confirm":true,"band_id":"fm"}),
        )
        .await
        .unwrap();
    let b = h
        .service
        .create_baseline(
            id,
            json!({"name":"B","location":"Room","confirm":true,"band_id":"uhf"}),
        )
        .await
        .unwrap();
    let mut bursts = vec![];
    for (band, index) in [("fm", 0), ("uhf", 20)] {
        let mut burst = quiet[index].clone();
        burst["id"] = json!(format!("burst-{band}"));
        burst["interval"] = f["sweeps"][20]["interval"].clone();
        burst["levels"] = json!([-90., -90., -90., -90., -40., -90., -90., -90., -90.]);
        bursts.push(burst);
    }
    h.archive.append_sweeps(bursts).await.unwrap();
    let result = h.service.analyze(id).await.unwrap();
    assert_eq!(result["processed_sweeps"], 2);
    assert_eq!(result["quality_issues"], 0);
    assert_eq!(result["events_updated"], 2);
    let events = h
        .archive
        .list_monitoring(
            "event",
            MonitoringQuery {
                project_id: Some(id.into()),
                ..Default::default()
            },
        )
        .await
        .unwrap();
    assert_eq!(events.len(), 2);
    let low = events
        .iter()
        .find(|e| e["scan_band_id"] == "fm")
        .expect("FM evidence");
    let high = events
        .iter()
        .find(|e| e["scan_band_id"] == "uhf")
        .expect("UHF evidence");
    assert_eq!(low["baseline_id"], a["id"]);
    assert_eq!(high["baseline_id"], b["id"]);
    assert_eq!(low["source_count"], 1);
    assert_eq!(high["source_count"], 1);
    assert_eq!(low["state"], "open");
    assert_eq!(high["state"], "open");
    let comparison = h.service.history_comparison(id, "burst-uhf").await.unwrap();
    assert_eq!(comparison["baseline_id"], b["id"]);
    assert_eq!(comparison["compatible"], true);
    assert_eq!(h.service.analyze(id).await.unwrap()["processed_sweeps"], 0);
    h.close().await;
}

async fn prepared_cycle(h: &Harness) -> String {
    let p = h
        .service
        .create(json!({"name":"Cycle", "site":"Room", "session_id":fixture()["session"]["id"]}))
        .await
        .unwrap();
    let id = p["id"].as_str().unwrap();
    let mut config = proposal();
    for (i, start) in [100000000u64, 200000000].into_iter().enumerate() {
        config["bands"][i]["start_hz"] = json!(start);
        config["bands"][i]["stop_hz"] = json!(start + 80000);
        config["bands"][i]["points"] = json!(9);
    }
    let p = h
        .service
        .configure(id, json!({"scan_plan":config}))
        .await
        .unwrap();
    h.service.save_preparation(id,json!({"antenna_notes":"RX fixture", "receive_path_confirmed":true,"location_confirmed":true})).await.unwrap();
    for band in ["fm", "uhf"] {
        let mut sources = vec![];
        for i in 0..20 {
            let mut source = cycle_sample(&p, band, &format!("quiet-{band}-{i}"), i as i64);
            source["interval"] = fixture()["sweeps"][i]["interval"].clone();
            sources.push(source);
        }
        h.archive.append_sweeps(sources).await.unwrap();
        h.service
            .create_baseline(
                id,
                json!({"name":band,"location":"Room","confirm":true,"band_id":band}),
            )
            .await
            .unwrap();
    }
    id.into()
}

fn cycle_sample(project: &Value, band: &str, id: &str, seconds: i64) -> Value {
    use chrono::{DateTime, Duration, SecondsFormat};
    let mut raw = fixture()["sweeps"][0].clone();
    let first = if band == "fm" {
        100000000u64
    } else {
        200000000
    };
    raw["id"] = json!(id);
    raw["session_id"] = project["active_session_id"].clone();
    raw["stream_id"] = project["analyzer_stream_id"].clone();
    raw["frequencies_hz"] = json!((0..9).map(|i| first + i * 10000).collect::<Vec<_>>());
    raw["levels"] = json!(vec![-90.; 9]);
    raw["settings"]["rbw_hz"] = json!(if band == "fm" { 10000 } else { 30000 });
    raw["settings"]["scan_plan_id"] = project["scan_plan"]["id"].clone();
    raw["settings"]["scan_band_id"] = json!(band);
    let start = DateTime::parse_from_rfc3339(fixture()["clock"].as_str().unwrap()).unwrap()
        + Duration::seconds(seconds);
    raw["interval"] = json!({"started_at":start.to_rfc3339_opts(SecondsFormat::Micros,true),"ended_at":(start+Duration::seconds(1)).to_rfc3339_opts(SecondsFormat::Micros,true)});
    raw
}

#[tokio::test]
async fn scan_plan_parameter_shrink_discards_old_window_before_next_analysis() {
    let h = Harness::new().await;
    let id = prepared_cycle(&h).await;
    let p = h.service.project(&id).await.unwrap();
    h.archive
        .append_sweeps(
            (0..5)
                .map(|i| cycle_sample(&p, "fm", &format!("sample-{i}"), i))
                .collect(),
        )
        .await
        .unwrap();
    h.service.analyze(&id).await.unwrap();
    assert!(!h.service.project(&id).await.unwrap()["band_detectors"]["fm"].is_null());
    h.service
        .update_settings(
            &id,
            json!({"detector":{"window_sweeps":3,"confirmations":2}}),
        )
        .await
        .unwrap();
    let updated = h.service.project(&id).await.unwrap();
    assert_eq!(
        updated["band_detectors"],
        json!({}),
        "old 5-sample window cannot enter new 3-sample parameters"
    );
    h.archive
        .append_sweeps(vec![cycle_sample(&updated, "fm", "after-shrink", 5)])
        .await
        .unwrap();
    assert_eq!(h.service.analyze(&id).await.unwrap()["processed_sweeps"], 1);
    h.close().await;
}

#[tokio::test]
async fn scan_plan_rearm_cannot_reuse_previous_session_confirmation_evidence() {
    let h = Harness::new().await;
    let id = prepared_cycle(&h).await;
    let p = h.service.arm(&id, json!({})).await.unwrap();
    h.archive
        .append_sweeps(vec![cycle_sample(&p, "fm", "old-session", 0)])
        .await
        .unwrap();
    h.service.analyze(&id).await.unwrap();
    assert!(!h.service.project(&id).await.unwrap()["band_detectors"]["fm"].is_null());
    h.service.disarm(&id, json!({})).await.unwrap();
    let p = h.service.arm(&id, json!({})).await.unwrap();
    assert_eq!(
        p["band_detectors"],
        json!({}),
        "new session must start without old evidence or adaptive state"
    );
    h.archive
        .append_sweeps(vec![cycle_sample(&p, "fm", "new-session", 1)])
        .await
        .unwrap();
    assert_eq!(h.service.analyze(&id).await.unwrap()["processed_sweeps"], 1);
    h.close().await;
}

#[tokio::test]
async fn scan_plan_manual_return_requires_fresh_evidence_from_all_bands_in_any_order() {
    use std::time::Duration;
    let h = Harness::new().await;
    let id = prepared_cycle(&h).await;
    let p = h.service.arm(&id, json!({})).await.unwrap();
    h.archive
        .append_sweeps(vec![cycle_sample(&p, "fm", "before-manual", 0)])
        .await
        .unwrap();
    h.service.analyze(&id).await.unwrap();
    let lease = h
        .service
        .manual_acquire(&id, json!({"role":"analyzer"}))
        .await
        .unwrap();
    *h.control.sweep_on_start.lock().unwrap() =
        Some((h.archive.clone(), cycle_sample(&p, "uhf", "return-uhf", 1)));
    h.service
        .manual_return(&id, json!({"token":lease["token"]}))
        .await
        .unwrap();
    // Let the owned restore callback observe the non-first band by itself.
    tokio::time::sleep(Duration::from_millis(80)).await;
    let restoring = h.service.project(&id).await.unwrap();
    assert_ne!(
        restoring["state"], "error",
        "a valid non-first band must not be compared to the first baseline: {restoring}"
    );
    assert_ne!(
        restoring["coverage"], "wide",
        "one band alone cannot restore all coverage"
    );
    h.archive
        .append_sweeps(vec![cycle_sample(&p, "fm", "return-fm", 2)])
        .await
        .unwrap();
    tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            let current = h.service.project(&id).await.unwrap();
            assert_ne!(current["state"], "error", "{current}");
            if current["coverage"] == "wide" {
                assert_eq!(current["band_detectors"], json!({}));
                break;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    h.close().await;
}
