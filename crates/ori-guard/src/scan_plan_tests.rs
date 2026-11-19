use crate::test_common::*;
use serde_json::json;

#[tokio::test]
async fn narrow_source_search_does_not_inherit_the_cyclic_wide_scan_plan() {
    let h = Harness::new().await;
    let p = h
        .service
        .create(json!({"name":"Survey","site":"Room","session_id":fixture()["session"]["id"]}))
        .await
        .unwrap();
    let id = p["id"].as_str().unwrap();
    let p=h.service.configure(id,json!({"scan_plan":{"name":"Cycle","bands":[
        {"id":"fm","name":"FM","start_hz":100000000,"stop_hz":101000000,"points":201,"rbw_hz":10000,"max_revisit_seconds":15.0}
    ]}})).await.unwrap();
    let wide = h.service.clone_session(&p, true, None).unwrap();
    assert_eq!(
        wide["streams"][0]["settings"]["scan_plan"]["id"],
        p["scan_plan"]["id"]
    );
    let narrow = h
        .service
        .clone_session(
            &p,
            false,
            Some(&json!({"center_frequency_hz":100040000,"span_hz":20000})),
        )
        .unwrap();
    assert!(
        narrow["streams"][0]["settings"].get("scan_plan").is_none(),
        "a narrow worker would otherwise restart all wide bands"
    );
    assert_eq!(narrow["streams"][0]["settings"]["start_hz"], 100030000);
    assert_eq!(narrow["streams"][0]["settings"]["stop_hz"], 100050000);
    assert_eq!(narrow["streams"].as_array().unwrap().len(), 1);
    assert!(h.control.calls.lock().unwrap().is_empty());
    h.close().await;
}

#[tokio::test]
async fn narrow_upper_band_uses_independent_detailed_grid_inside_explicit_span() {
    let h = Harness::new().await;
    let project = h
        .service
        .create(json!({"name":"RX","site":"Synthetic","session_id":fixture()["session"]["id"]}))
        .await
        .unwrap();
    let id = project["id"].as_str().unwrap();
    let p=h.service.configure(id,json!({"scan_plan":{"name":"Wide","bands":[{
        "id":"whole","name":"Whole","start_hz":100000,"stop_hz":1300000000,"points":129991,"step_hz":10000,
        "rbw_hz":30000,"max_revisit_seconds":3600}]}})).await.unwrap();
    let narrow = h
        .service
        .clone_session(
            &p,
            false,
            Some(&json!({"center_frequency_hz":1299990000,"span_hz":60000})),
        )
        .unwrap();
    let record = &narrow["streams"][0]["settings"]["scan_plan"];
    assert_eq!(record["id"], narrow["id"]);
    assert_ne!(record["id"], p["scan_plan"]["id"]);
    let band = &record["config"]["bands"][0];
    assert_eq!(band["start_hz"], 1299960000);
    assert_eq!(band["stop_hz"], 1300000000);
    assert_eq!(band["points"], 5);
    assert_eq!(band["rbw_hz"], 10000);
    assert!(
        h.service
            .clone_session(
                &p,
                false,
                Some(&json!({"center_frequency_hz":1300000000,"span_hz":1000}))
            )
            .is_err()
    );
    h.close().await;
}
