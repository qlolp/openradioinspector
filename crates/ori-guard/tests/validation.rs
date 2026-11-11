//! Actual Python API validation/error ordering over the same owned SQLite data.
mod common;
use common::*;
use ori_archive::{MonitoringQuery, Mutation};
use ori_guard::{GuardError, Result};
use serde_json::{Value, json};

fn validation_fixture() -> Value {
    ori_contracts::parse_json_value(
        include_bytes!("../../../contracts/fixtures/guard-validation-v1.json"),
        1024 * 1024,
    )
    .unwrap()
}

fn sqlite_bytes(h: &Harness) -> [Vec<u8>; 2] {
    ["archive.sqlite", "archive.sqlite-wal"].map(|name| {
        let path = h.dir.path().join(name);
        match std::fs::read(&path) {
            Ok(bytes) => bytes,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Vec::new(),
            Err(error) => panic!("cannot inspect {}: {error}", path.display()),
        }
    })
}

async fn capture(h: &Harness) -> Value {
    let mut records = json!({});
    for kind in [
        "project",
        "baseline",
        "allowed",
        "event",
        "event-source",
        "quality",
        "position-mark",
        "source-search",
    ] {
        records[kind] = json!(
            h.archive
                .list_monitoring(
                    kind,
                    MonitoringQuery {
                        limit: 1000,
                        ..Default::default()
                    }
                )
                .await
                .unwrap()
        );
    }
    json!({"records":records,"counts":h.archive.registry_counts().await.unwrap()})
}

async fn raw_capture(h: &Harness) -> Value {
    json!({"events":h.archive.events(0,1000).await.unwrap(),"sweeps":h.archive.list_sweeps(fixture()["session"]["id"].as_str().unwrap(),None,100).await.unwrap()})
}

async fn invoke(h: &Harness, case: &Value) -> Result<Value> {
    let project = case["project_id"].as_str().unwrap();
    let event = case["event_id"].as_str().unwrap();
    let payload = case["payload"].clone();
    match case["operation"].as_str().unwrap() {
        "project" => h.service.project(project).await,
        "create_baseline" => h.service.create_baseline(project, payload).await,
        "add_allowed" => h.service.add_allowed(project, payload).await,
        "review_event" => h.service.review_event(project, event, payload).await,
        "get_event" => h.service.get_record("event", event, Some(project)).await,
        "event_sources" => h.service.event_sources(project, event, "", 64).await,
        "event_audio" => h
            .service
            .event_audio(project, event)
            .await
            .map(|_| panic!("owned fixture has no audio")),
        "history_original" => h.service.history_original(project, event).await,
        "acknowledge_alert" => h.service.acknowledge_alert(project, event, payload).await,
        "save_preparation" => h.service.save_preparation(project, payload).await,
        "manual_acquire" => h.service.manual_acquire(project, payload).await,
        "manual_renew" => h.service.manual_renew(project, payload).await,
        "manual_return" => h.service.manual_return(project, payload).await,
        "manual_tune" => h.service.manual_tune(project, payload).await,
        "search_start" => h.service.search_start(project, payload).await,
        "search_mark" => h.service.search_mark(project, payload).await,
        other => panic!("unknown owned fixture action {other}"),
    }
}

fn response(result: Result<Value>, operation: &str) -> Value {
    match result {
        Ok(data) => {
            json!({"status":if ["create_baseline","add_allowed"].contains(&operation){201}else{200},"body":{"contract":"openradioinspector-platform","contract_version":1,"data":data}})
        }
        Err(error) => {
            json!({"status":error.http_status(),"body":{"contract":"openradioinspector-platform","contract_version":1,"error":{"code":error.api_code(),"message":error.to_string()}}})
        }
    }
}

#[tokio::test]
async fn actual_python_lookup_validation_and_manual_precedence_preserve_sqlite() {
    let h = Harness::new().await;
    let f = validation_fixture();
    h.archive
        .append_sweeps(fixture()["sweeps"].as_array().unwrap()[..20].to_vec())
        .await
        .unwrap();
    for kind in ["project", "event"] {
        for record in f["records"][kind].as_array().unwrap() {
            h.archive
                .put_monitoring(kind, record.clone())
                .await
                .unwrap();
        }
    }
    let primary = f["records"]["project"][0].clone();
    let event = f["records"]["event"][0].clone();
    let mut full = false;
    let mut failures = Vec::new();
    let raw_before = raw_capture(&h).await;
    for case in f["cases"].as_array().unwrap() {
        let mut project = primary.clone();
        project["state"] = case["project_state"].clone();
        h.archive.put_monitoring("project", project).await.unwrap();
        h.archive
            .put_monitoring("event", event.clone())
            .await
            .unwrap();
        if case["catalog_full"] == true && !full {
            let rows:Vec<_> = (0..1000).map(|index|Mutation::Monitoring {kind:"allowed".into(),value:json!({"id":format!("full-{index:04}"),"project_id":primary["id"],"created_at":fixture()["clock"],"name":"Rule","start_hz":0,"end_hz":0,"comment":"","watch_changes":false})}).collect();
            for batch in rows.chunks(256) {
                h.archive.apply(batch.to_vec()).await.unwrap();
            }
            full = true;
        }
        let before = capture(&h).await;
        let sqlite_before = sqlite_bytes(&h);
        let actual = response(invoke(&h, case).await, case["operation"].as_str().unwrap());
        let after = capture(&h).await;
        if actual != case["expected"] {
            failures.push(format!(
                "{} expected {}, actual {}",
                case["case_id"], case["expected"], actual
            ));
        }
        if case["unchanged"] == true {
            assert_eq!(before, after, "{} changed archive state", case["case_id"]);
            assert_eq!(
                sqlite_before,
                sqlite_bytes(&h),
                "{} wrote to SQLite despite an unchanged response",
                case["case_id"]
            );
        } else {
            let mut expected = before;
            let rows = expected["records"]["event"].as_array_mut().unwrap();
            let target = rows
                .iter_mut()
                .find(|row| row["id"] == event["id"])
                .unwrap();
            *target = case["expected"]["body"]["data"].clone();
            assert_eq!(after, expected, "only the reviewed event may change");
            assert_eq!(
                actual["body"]["data"]["opaque"]["é"]
                    .as_f64()
                    .unwrap()
                    .to_bits(),
                (-0.0f64).to_bits()
            );
            assert_eq!(
                actual["body"]["data"]["opaque"]["é"]
                    .as_f64()
                    .unwrap()
                    .to_bits(),
                (-1.0000000000000002f64).to_bits()
            );
        }
        assert!(
            h.control.calls.lock().unwrap().is_empty()
                && h.control.workers.lock().unwrap().is_empty()
                && h.control.permits.lock().unwrap().is_empty()
                && h.control.audio_requests.lock().unwrap().is_empty()
                && h.control.deliveries.lock().unwrap().is_empty(),
            "validation must not start/tune/stop devices"
        );
    }
    assert_eq!(
        raw_capture(&h).await,
        raw_before,
        "original raw sweeps and archive events must remain unchanged"
    );
    h.close().await;
    assert!(
        failures.is_empty(),
        "{} mismatches:\n{}",
        failures.len(),
        failures.join("\n")
    );
}

#[tokio::test]
async fn missing_record_context_preserves_exact_identifier_without_public_invention() {
    let h = Harness::new().await;
    let id = "missing-é/%+?\"";
    let error = h.service.project(id).await.unwrap_err();
    assert_eq!(error.http_status(), 404);
    assert_eq!(error.api_code(), "monitoring_not_found");
    assert_eq!(error.to_string(), "'Запись наблюдения не найдена'");
    assert!(
        matches!(error, GuardError::RecordNotFound {kind, id: actual} if kind=="project" && actual==id)
    );
    let scoped = h
        .service
        .get_record("event", "missing-event", Some(id))
        .await
        .unwrap_err();
    assert!(
        matches!(scoped, GuardError::RecordNotFound {kind,id:actual} if kind=="project" && actual==id)
    );
    h.close().await;
}
