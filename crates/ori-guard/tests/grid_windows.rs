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
