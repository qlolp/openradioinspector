mod common;
use common::*;
use ori_archive::MonitoringQuery;
use serde_json::{Value, json};

#[tokio::test]
async fn python_impulse_peak_updates_broad_regions_and_watched_carriers_merge_with_exact_sources() {
    let h = Harness::new().await;
    let f = fixture();
    let merge: Value = ori_contracts::parse_json_value(
        include_bytes!("../../../contracts/fixtures/guard-merge-v1.json"),
        2 * 1024 * 1024,
    )
    .unwrap();
    let p = h
        .service
        .create(json!({"name":"Merge","site":"Fixture","session_id":f["session"]["id"]}))
        .await
        .unwrap();
    let id = p["id"].as_str().unwrap();
    h.service
        .add_allowed(
            id,
            json!({"name":"Watched","start_hz":100060000,"end_hz":100070000,"watch_changes":true}),
        )
        .await
        .unwrap();
    h.archive
        .append_sweeps(f["sweeps"].as_array().unwrap()[..20].to_vec())
        .await
        .unwrap();
    h.service
        .create_baseline(
            id,
            json!({"name":"Quiet","location":"Fixture","confirm":true}),
        )
        .await
        .unwrap();
    for (index, stage) in merge["stages"].as_array().unwrap().iter().enumerate() {
        h.archive
            .append_sweeps(stage["sweeps"].as_array().unwrap().clone())
            .await
            .unwrap();
        assert_eq!(
            h.service.analyze(id).await.unwrap(),
            stage["report"],
            "stage {index}"
        );
        for (kind, key) in [("event", "events"), ("event-source", "sources")] {
            let values = h
                .archive
                .list_monitoring(
                    kind,
                    MonitoringQuery {
                        project_id: Some(id.into()),
                        limit: 1000,
                        ..Default::default()
                    },
                )
                .await
                .unwrap();
            assert_eq!(json!(values), stage[key], "stage {index} {kind}");
        }
    }
    h.close().await;
}
