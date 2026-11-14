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
