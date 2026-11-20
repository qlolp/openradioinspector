mod common;
use common::{Harness, fixture};
use serde_json::json;

#[tokio::test]
async fn archived_pairs_preserve_signal_and_discrepancy_as_independent_results() {
    let h = Harness::new().await;
    let session = fixture()["session"].clone();
    let project = h
        .service
        .create(json!({"name":"Training","site":"Fixture","session_id":session["id"]}))
        .await
        .unwrap();
    let id = project["id"].as_str().unwrap();
    let sid = session["id"].as_str().unwrap();
    let center = 100_000_000;
    let mut sweeps = Vec::new();
    let mut observations = Vec::new();
    let mut pair = |key: &str, offset: i64, tick: usize, level: f64, counts: &[i64]| {
        let hz = center + offset;
        let interval = json!({"started_at":format!("2026-01-02T00:00:{tick:02}.000Z"),"ended_at":format!("2026-01-02T00:00:{tick:02}.900Z")});
        let sweep_id = format!("s-{key}");
        sweeps.push(
            json!({"id":sweep_id,"session_id":sid,"stream_id":project["analyzer_stream_id"],
            "level_unit":"dBm","frequencies_hz":[hz],"levels":[level],"interval":interval,
            "settings":{"adapter":"tinysa-sim","simulated":true,"rbw_hz":10000}}),
        );
        let mut ids = Vec::new();
        for (n, count) in counts.iter().enumerate() {
            let obs_id = format!("o-{key}-{n}");
            observations.push(json!({"id":obs_id,"session_id":sid,"stream_id":project["receiver_stream_id"],
                "frequency_hz":hz,"mode":"FM","level":count,"level_unit":"s_meter_count","interval":interval,
                "metadata":{"adapter":"ardv1-sim","simulated":true}}));
            ids.push(obs_id);
        }
        json!({"sweep_id":sweep_id,"observation_ids":ids})
    };
    let initial = pair("initial", 0, 6, -97.0, &[82]);
    let neighbors = [-60, -50, -40, -30, -20, 20, 30, 40, 50, 60]
        .into_iter()
        .enumerate()
        .map(|(i, n)| {
            pair(
                &format!("neighbor-{i}"),
                n * 1000,
                i + 1,
                -97.0,
                &[116, 117],
            )
        })
        .collect::<Vec<_>>();
    let lower = pair("lower", -100_000, 13, -106.9375, &[116]);
    let upper = pair("upper", 100_000, 17, -106.9375, &[117]);
    let first = pair("first", 0, 14, -109.0625, &[82]);
    let second = pair("second", 0, 16, -110.5625, &[82]);
    let recipe = json!({"initial":initial,"neighbors":neighbors,"controls":{"lower":lower,"upper":upper},"repeats":[first,second]});
    let malformed_recipe = {
        let mut value = recipe.clone();
        value["initial"]["tiny_delta_db"] = json!(30);
        value
    };
    h.archive.append_sweeps(sweeps).await.unwrap();
    h.archive.append_observations(observations).await.unwrap();
    let record = json!({"id":"event-paired","project_id":id,"created_at":"2026-01-02T00:00:06Z","first_seen":"2026-01-02T00:00:06Z","last_seen":"2026-01-02T00:00:07Z","run_session_id":sid,
        "center_frequency_hz":center,"peak_level":-97.0});
    h.archive
        .put_monitoring("event", record.clone())
        .await
        .unwrap();
    let mut peak = record.clone();
    peak["session_id"] = json!(sid);
    peak["stream_id"] = project["analyzer_stream_id"].clone();
    peak["sweep_id"] = initial["sweep_id"].clone();
    // Legacy archives may contain the source index without a dedicated peak row.
    let mut legacy = peak.clone();
    legacy["id"] = json!("event-paired.s-initial");
    legacy["event_id"] = json!("event-paired");
    h.archive
        .put_monitoring("event-source", legacy)
        .await
        .unwrap();
    assert_eq!(
        h.service.paired_rechecks(id, "event-paired").await.unwrap()["state"],
        "missing_evidence"
    );
    for bad in [
        malformed_recipe,
        {
            let mut v = recipe.clone();
            v["repeats"][1] = v["repeats"][0].clone();
            v
        },
        {
            let mut v = recipe.clone();
            v["repeats"][0]["observation_ids"] = v["repeats"][1]["observation_ids"].clone();
            v
        },
        {
            let mut v = recipe.clone();
            v["controls"]["upper"] = v["neighbors"][7].clone();
            v
        },
    ] {
        assert!(
            h.service
                .save_paired_rechecks(id, "event-paired", bad)
                .await
                .is_err()
        );
    }
    let result = h
        .service
        .save_paired_rechecks(id, "event-paired", recipe.clone())
        .await
        .unwrap();
    assert_eq!(result["signal_repeats"], 2);
    assert_eq!(result["discrepancy_repeats"], 1);
    assert_eq!(result["repeat_total"], 2);
    assert_eq!(result["instrument"], "AR-DV1");
    assert_eq!(result["visits"][0]["ar_drop_counts"], 34.5);
    assert_eq!(result["visits"][0]["tiny_delta_db"], -2.125);
    assert_eq!(result["visits"][1]["tiny_delta_db"], -3.625);
    assert_eq!(result["visits"][1]["discrepancy_repeated"], false);
    assert_eq!(result["simulated"], true);
    assert_eq!(
        h.service.paired_rechecks(id, "event-paired").await.unwrap(),
        result
    );
    let mut second_recipe = recipe.clone();
    second_recipe["repeats"].as_array_mut().unwrap().pop();
    let newer = h
        .service
        .save_paired_rechecks(id, "event-paired", second_recipe)
        .await
        .unwrap();
    assert_eq!(
        h.service
            .save_paired_rechecks(id, "event-paired", recipe)
            .await
            .unwrap(),
        result
    );
    assert_eq!(
        h.service.paired_rechecks(id, "event-paired").await.unwrap(),
        newer
    );
    // A new strongest source changes the event binding. The previous immutable
    // research result remains retrievable as an archive record.
    let mut retargeted = record.clone();
    retargeted["peak_level"] = json!(-70.);
    h.archive.put_monitoring("event", retargeted).await.unwrap();
    peak["sweep_id"] = json!("another-peak");
    peak["peak_level"] = json!(-70.);
    h.archive.put_monitoring("event-peak", peak).await.unwrap();
    assert_eq!(
        h.service.paired_rechecks(id, "event-paired").await.unwrap()["state"],
        "missing_evidence"
    );
    assert!(
        h.archive
            .monitoring("paired-recheck", result["id"].as_str().unwrap())
            .await
            .unwrap()
            .is_some()
    );
    h.close().await;
}
