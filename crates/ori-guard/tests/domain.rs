mod common;
use common::*;
use ori_archive::MonitoringQuery;
use serde_json::json;

#[tokio::test]
async fn python_exact_project_baseline_events_sources_hysteresis_and_gap() {
    let h = Harness::new().await;
    let f = fixture();
    let p = h
        .service
        .create(json!({"name":"Room α","site":"Cité+%/?","session_id":f["session"]["id"]}))
        .await
        .unwrap();
    assert_eq!(p, f["created"]);
    let id = p["id"].as_str().unwrap();
    assert_eq!(h.service.save_preparation(id,json!({"antenna_notes":"RX fixture","receive_path_confirmed":true,"location_confirmed":true})).await.unwrap(),f["preparation"]);
    assert_eq!(h.service.add_allowed(id,json!({"name":"Known carrier","start_hz":100060000,"end_hz":100070000,"comment":"Original units","watch_changes":true})).await.unwrap(),f["allowed"]);
    let sweeps = f["sweeps"].as_array().unwrap();
    h.archive
        .append_sweeps(sweeps[..20].to_vec())
        .await
        .unwrap();
    assert_eq!(
        h.service
            .create_baseline(id, json!({"name":"Quiet","location":"Roof","confirm":true}))
            .await
            .unwrap(),
        f["baseline"]
    );
    assert_eq!(h.service.project(id).await.unwrap(), f["prepared_project"]);
    h.archive
        .append_sweeps(sweeps[20..23].to_vec())
        .await
        .unwrap();
    assert_eq!(h.service.analyze(id).await.unwrap(), f["first_analysis"]);
    let event_id = f["first_event"]["id"].as_str().unwrap();
    assert_eq!(
        h.archive
            .monitoring("event", event_id)
            .await
            .unwrap()
            .unwrap(),
        f["first_event"]
    );
    assert_eq!(
        h.service
            .review_event(
                id,
                event_id,
                json!({"disposition":"false_positive","comment":"Known fixture"})
            )
            .await
            .unwrap(),
        f["reviewed_event"]
    );
    h.archive
        .append_sweeps(sweeps[23..28].to_vec())
        .await
        .unwrap();
    assert_eq!(h.service.analyze(id).await.unwrap(), f["quiet_analysis"]);
    h.archive
        .append_sweeps(sweeps[28..].to_vec())
        .await
        .unwrap();
    assert_eq!(h.service.analyze(id).await.unwrap(), f["gap_analysis"]);
    assert_eq!(h.service.analyze(id).await.unwrap(), f["replay_analysis"]);
    for kind in [
        "project",
        "baseline",
        "allowed",
        "event",
        "event-source",
        "quality",
    ] {
        let actual = h
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
        assert_eq!(json!(actual), f["records"][kind], "{kind}");
    }
    assert!(
        h.control.calls.lock().unwrap().is_empty(),
        "offline analysis must not start/tune devices"
    );
    h.close().await;
}

#[tokio::test]
async fn operator_confirmation_and_exact_units_are_required_without_partial_records() {
    let h = Harness::new().await;
    let f = fixture();
    let p = h
        .service
        .create(json!({"name":"Check","site":"Site","session_id":f["session"]["id"]}))
        .await
        .unwrap();
    let id = p["id"].as_str().unwrap();
    h.archive
        .append_sweeps(f["sweeps"].as_array().unwrap()[..20].to_vec())
        .await
        .unwrap();
    for confirm in [json!(false), json!(1), json!("true"), json!(null)] {
        assert!(
            h.service
                .create_baseline(
                    id,
                    json!({"name":"Quiet","location":"Roof","confirm":confirm})
                )
                .await
                .is_err()
        );
    }
    assert!(
        h.archive
            .list_monitoring("baseline", MonitoringQuery::default())
            .await
            .unwrap()
            .is_empty()
    );
    for payload in [
        json!({"name":"Rule","start_hz":1.0,"end_hz":2}),
        json!({"name":"Rule","start_hz":true,"end_hz":2}),
        json!({"name":"Rule","start_hz":2,"end_hz":1}),
        json!({"name":"Rule","start_hz":0,"end_hz":20_000_000_001u64}),
        json!({"name":"Rule","start_hz":0,"end_hz":2,"watch_changes":1}),
    ] {
        assert!(h.service.add_allowed(id, payload).await.is_err());
    }
    assert!(
        h.archive
            .list_monitoring("allowed", MonitoringQuery::default())
            .await
            .unwrap()
            .is_empty()
    );
    let mut session = f["session"].clone();
    session["id"] = json!("uncalibrated");
    for stream in session["streams"].as_array_mut().unwrap() {
        stream["session_id"] = json!("uncalibrated");
        stream["id"] = json!(format!("other-{}", stream["id"].as_str().unwrap()));
    }
    session["streams"][1]["level_unit"] = json!("dBm");
    h.archive.create_session(session).await.unwrap();
    assert!(
        h.service
            .create(json!({"name":"Invalid units","site":"Site","session_id":"uncalibrated"}))
            .await
            .is_err()
    );
    h.close().await;
}

#[tokio::test]
async fn preflight_unknown_effects_fail_closed_and_replacement_collection_ignores_old_baseline() {
    let h = Harness::new().await;
    let id = h.prepared().await;
    assert_eq!(
        h.service.preflight(&id, false).await.unwrap()["ready"],
        true
    );
    *h.control.ready.lock().unwrap() = None;
    assert_eq!(
        h.service.preflight(&id, false).await.unwrap()["ready"],
        false
    );
    *h.control.ready.lock().unwrap() = Some(true);
    h.control.storage.lock().unwrap().writable = None;
    assert_eq!(
        h.service.preflight(&id, false).await.unwrap()["ready"],
        false
    );
    h.control.storage.lock().unwrap().writable = Some(true);
    let mut p = h.service.project(&id).await.unwrap();
    p["baseline_id"] = json!("missing-baseline");
    h.archive.put_monitoring("project", p).await.unwrap();
    assert_eq!(
        h.service.preflight(&id, false).await.unwrap()["ready"],
        false
    );
    assert_eq!(h.service.preflight(&id, true).await.unwrap()["ready"], true);
    h.close().await;
}

#[tokio::test]
async fn event_scope_and_catalog_limits_do_not_leak_other_projects() {
    let h = Harness::new().await;
    let id = h.prepared().await;
    let f = fixture();
    h.archive
        .append_sweeps(f["sweeps"].as_array().unwrap()[20..23].to_vec())
        .await
        .unwrap();
    h.service.analyze(&id).await.unwrap();
    let other = h
        .service
        .create(json!({"name":"Other","site":"Site","session_id":f["session"]["id"]}))
        .await
        .unwrap();
    let other = other["id"].as_str().unwrap();
    let event = f["first_event"]["id"].as_str().unwrap();
    assert!(
        h.service
            .review_event(other, event, json!({"disposition":"authorized"}))
            .await
            .is_err()
    );
    assert!(h.service.event_sources(other, event, "", 64).await.is_err());
    let sources = h.service.event_sources(&id, event, "", 2).await.unwrap();
    assert_eq!(sources["items"].as_array().unwrap().len(), 2);
    assert!(sources["next_cursor"].is_string());
    let tail = h
        .service
        .event_sources(&id, event, sources["next_cursor"].as_str().unwrap(), 2)
        .await
        .unwrap();
    assert_eq!(tail["items"].as_array().unwrap().len(), 1);
    assert!(tail["next_cursor"].is_null());
    h.close().await;
}

#[tokio::test]
async fn legacy_cadence_uses_reference_boundaries_without_rewriting_frozen_baseline() {
    let h = Harness::new().await;
    let id = h.prepared().await;
    let p = h.service.project(&id).await.unwrap();
    let baseline_id = p["baseline_id"].as_str().unwrap();
    let mut b = h
        .archive
        .monitoring("baseline", baseline_id)
        .await
        .unwrap()
        .unwrap();
    let refs = b["source_refs"].as_array().unwrap();
    let mut missing = refs[0].clone();
    missing["sweep_id"] = json!("missing-source");
    b["source_refs"] = json!([refs[0], missing, refs[10], refs[11]]);
    b["sweep_period_seconds"] = serde_json::Value::Null;
    h.archive
        .put_monitoring("baseline", b.clone())
        .await
        .unwrap();
    let derived = h.service.detection_baseline(b.clone()).await.unwrap();
    assert_eq!(derived["sweep_period_seconds"], 4.0);
    assert_eq!(derived["detector_version"], b["detector_version"]);
    assert_eq!(
        h.archive
            .monitoring("baseline", baseline_id)
            .await
            .unwrap()
            .unwrap(),
        b
    );
    let mut too_many = b;
    too_many["source_refs"] = json!(vec![derived["source_refs"][0].clone(); 257]);
    assert!(h.service.detection_baseline(too_many).await.is_err());
    h.close().await;
}

#[tokio::test]
async fn twenty_fast_or_incompatible_sweeps_cannot_create_a_baseline() {
    for incompatible in [false, true] {
        let h = Harness::new().await;
        let f = fixture();
        let p = h
            .service
            .create(json!({"name":"Check","site":"Site","session_id":f["session"]["id"]}))
            .await
            .unwrap();
        let id = p["id"].as_str().unwrap();
        let mut sweeps = f["sweeps"].as_array().unwrap()[..20].to_vec();
        if incompatible {
            sweeps[19]["settings"]["rbw_hz"] = json!(30000);
        } else {
            for (i, s) in sweeps.iter_mut().enumerate() {
                s["interval"] = json!({"started_at":format!("2026-11-11T12:00:00.{:06}Z",i*1000),"ended_at":format!("2026-11-11T12:00:00.{:06}Z",i*1000+500)});
            }
        }
        h.archive.append_sweeps(sweeps).await.unwrap();
        assert!(
            h.service
                .create_baseline(
                    id,
                    json!({"name":"Invalid","location":"Fixture","confirm":true})
                )
                .await
                .is_err()
        );
        assert!(
            h.archive
                .list_monitoring("baseline", MonitoringQuery::default())
                .await
                .unwrap()
                .is_empty()
        );
        assert_eq!(
            h.service.project(id).await.unwrap()["baseline_id"],
            serde_json::Value::Null
        );
        h.close().await;
    }
}
