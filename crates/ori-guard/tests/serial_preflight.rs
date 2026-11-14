mod common;
use common::*;
use serde_json::{Value, json};

#[tokio::test]
async fn python_conservative_serial_context_is_verified_without_opening_ports_or_claiming_readback()
{
    let f: Value = ori_contracts::parse_json_value(
        include_bytes!("../../../contracts/fixtures/guard-serial-preflight-v1.json"),
        1024 * 1024,
    )
    .unwrap();
    for case in f["cases"].as_array().unwrap() {
        let h = Harness::with_session(case["session"].clone()).await;
        let p=h.service.create(json!({"name":"Serial policy","site":"Synthetic only","session_id":case["session"]["id"]})).await.unwrap();
        let id = p["id"].as_str().unwrap();
        let undeclared = h.service.preflight(id, false).await.unwrap();
        assert_eq!(undeclared["ready"], case["undeclared"]["ready"]);
        h.service.save_preparation(id,json!({"antenna_notes":"Synthetic RX","receive_path_confirmed":true,"location_confirmed":true})).await.unwrap();
        h.archive
            .append_sweeps(case["sweeps"].as_array().unwrap().to_vec())
            .await
            .unwrap();
        h.service
            .create_baseline(
                id,
                json!({"name":"Serial","location":"Fixture","confirm":true}),
            )
            .await
            .unwrap();
        let actual = h.service.preflight(id, false).await.unwrap();
        assert_eq!(actual["ready"], case["prepared"]["ready"]);
        assert_eq!(actual["hardware_validation"], "not_run");
        for expected in case["prepared"]["checks"].as_array().unwrap() {
            let got = actual["checks"]
                .as_array()
                .unwrap()
                .iter()
                .find(|c| c["name"] == expected["name"])
                .unwrap_or_else(|| panic!("missing preflight check {expected}"));
            assert_eq!(got["ok"], expected["ok"], "{}", expected["name"]);
        }
        assert!(h.control.calls.lock().unwrap().is_empty());
        h.close().await;
    }
}
