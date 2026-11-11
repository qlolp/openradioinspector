mod common;
use common::*;
use ori_guard::{ControlTarget, GuardService};
use serde_json::json;

#[tokio::test]
async fn arm_claims_both_devices_and_manual_receiver_expiry_never_resumes_automation() {
    let h = Harness::new().await;
    let id = h.prepared().await;
    let armed = h.service.arm(&id, json!({})).await.unwrap();
    assert_eq!(armed["state"], "armed");
    let sid = armed["active_session_id"].as_str().unwrap();
    let receiver = armed["receiver_stream_id"].as_str().unwrap();
    let target = ControlTarget {
        session_id: sid.into(),
        stream_id: receiver.into(),
    };
    assert!(h.service.authorize_control(&target, None).is_err());
    let old = h
        .control
        .permits
        .lock()
        .unwrap()
        .iter()
        .find(|(t, _)| t == &target)
        .unwrap()
        .1
        .clone();
    assert!(h.service.authorize_control(&target, Some(&old)).is_ok());
    let analyzer_target = ControlTarget {
        session_id: sid.into(),
        stream_id: armed["analyzer_stream_id"].as_str().unwrap().into(),
    };
    let analyzer_permit = h
        .control
        .permits
        .lock()
        .unwrap()
        .iter()
        .find(|(t, _)| t == &analyzer_target)
        .unwrap()
        .1
        .clone();
    let manual = h
        .service
        .manual_acquire(&id, json!({"role":"receiver"}))
        .await
        .unwrap();
    let token = manual["token"].as_str().unwrap();
    assert!(
        h.service
            .authorize_control(&analyzer_target, Some(&analyzer_permit))
            .is_ok(),
        "receiver handoff must not revoke analyzer ownership"
    );
    assert!(h.service.authorize_control(&target, Some(&old)).is_err());
    h.service
        .manual_tune(
            &id,
            json!({"token":token,"frequency_hz":145510000,"mode":"FM"}),
        )
        .await
        .unwrap();
    h.sources.advance(31);
    assert!(
        h.service
            .manual_renew(&id, json!({"token":token}))
            .await
            .is_err()
    );
    assert!(
        h.service
            .manual_tune(
                &id,
                json!({"token":token,"frequency_hz":145520000,"mode":"FM"})
            )
            .await
            .is_err()
    );
    assert!(h.service.authorize_control(&target, Some(&old)).is_err());
    assert_eq!(
        h.service.guard_status(&id).await.unwrap()["manual"][0]["expired"],
        true
    );
    assert_eq!(
        h.service
            .manual_return(&id, json!({"token":token}))
            .await
            .unwrap(),
        json!({"state":"returned","role":"receiver"})
    );
    assert!(
        h.service.authorize_control(&target, Some(&old)).is_err(),
        "stale actor permit must never revive after explicit return"
    );
    assert!(
        h.service
            .authorize_control(&analyzer_target, Some(&analyzer_permit))
            .is_ok()
    );
    assert!(
        h.control
            .calls
            .lock()
            .unwrap()
            .iter()
            .any(|x| x == &format!("tune:{receiver}:145500000:AUTO"))
    );
    h.service.disarm(&id, json!({})).await.unwrap();
    assert!(h.service.authorize_control(&target, None).is_ok());
    assert!(h.service.authorize_control(&target, Some(&old)).is_err());
    h.close().await;
}

#[tokio::test]
async fn receiver_worker_replacement_requires_explicit_return() {
    let h = Harness::new().await;
    let id = h.prepared().await;
    let p = h.service.arm(&id, json!({})).await.unwrap();
    let lease = h
        .service
        .manual_acquire(&id, json!({"role":"receiver"}))
        .await
        .unwrap();
    let stream = p["receiver_stream_id"].as_str().unwrap();
    h.control
        .workers
        .lock()
        .unwrap()
        .iter_mut()
        .find(|w| w.stream_id == stream)
        .unwrap()
        .worker_instance = Some("replacement".into());
    assert!(
        h.service
            .manual_renew(&id, json!({"token":lease["token"]}))
            .await
            .is_err()
    );
    assert!(
        h.service
            .manual_tune(
                &id,
                json!({"token":lease["token"],"frequency_hz":145500000,"mode":"FM"})
            )
            .await
            .is_err()
    );
    assert_eq!(
        h.service
            .manual_return(&id, json!({"token":lease["token"]}))
            .await
            .unwrap()["state"],
        "returned"
    );
    h.close().await;
}

#[tokio::test]
async fn analyzer_manual_return_requires_a_fresh_compatible_wide_sweep() {
    let h = Harness::new().await;
    let id = h.prepared().await;
    let p = h.service.arm(&id, json!({})).await.unwrap();
    let receiver = p["receiver_stream_id"].as_str().unwrap();
    let manual = h
        .service
        .manual_acquire(&id, json!({"role":"analyzer"}))
        .await
        .unwrap();
    assert_eq!(h.service.project(&id).await.unwrap()["coverage"], "paused");
    assert!(
        h.control
            .workers
            .lock()
            .unwrap()
            .iter()
            .any(|w| w.stream_id == receiver && w.alive),
        "manual analyzer must not stop receiver"
    );
    let mut fresh = fixture()["sweeps"][0].clone();
    fresh["id"] = json!("fresh-return/%+");
    fresh["interval"] =
        json!({"started_at":fixture()["clock"],"ended_at":"2026-11-11T13:00:01.123456Z"});
    *h.control.sweep_on_start.lock().unwrap() = Some((h.archive.clone(), fresh));
    assert_eq!(
        h.service
            .manual_return(&id, json!({"token":manual["token"]}))
            .await
            .unwrap()["state"],
        "restoring"
    );
    tokio::time::timeout(std::time::Duration::from_secs(3), async {
        loop {
            let p = h.service.project(&id).await.unwrap();
            if p["coverage"] == "wide" {
                break;
            }
            assert_ne!(p["state"], "error", "{p}");
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    assert!(
        h.service.guard_status(&id).await.unwrap()["manual"]
            .as_array()
            .unwrap()
            .is_empty()
    );
    h.close().await;
}

#[tokio::test]
async fn preparation_claim_blocks_other_project_and_unknown_readiness_starts_nothing() {
    let h = Harness::new().await;
    let id = h.prepared().await;
    *h.control.ready.lock().unwrap() = None;
    assert!(h.service.arm(&id, json!({})).await.is_err());
    assert!(h.control.calls.lock().unwrap().is_empty());
    *h.control.ready.lock().unwrap() = Some(true);
    let collecting = h.service.start_preparation(&id, json!({})).await.unwrap();
    let p = &collecting["project"];
    let target = ControlTarget {
        session_id: p["active_session_id"].as_str().unwrap().into(),
        stream_id: p["analyzer_stream_id"].as_str().unwrap().into(),
    };
    assert!(h.service.authorize_control(&target, None).is_err());
    let other = h
        .service
        .create(json!({"name":"Other","site":"Shared","session_id":target.session_id}))
        .await
        .unwrap();
    let mut sweeps = fixture()["sweeps"].as_array().unwrap()[..20].to_vec();
    for sweep in &mut sweeps {
        sweep["id"] = json!(format!("copy-{}", sweep["id"].as_str().unwrap()));
        sweep["session_id"] = json!(target.session_id);
        sweep["stream_id"] = json!(target.stream_id);
    }
    h.archive.append_sweeps(sweeps).await.unwrap();
    let other = other["id"].as_str().unwrap();
    h.service
        .create_baseline(
            other,
            json!({"name":"Quiet","location":"Room","confirm":true}),
        )
        .await
        .unwrap();
    let calls = h.control.calls.lock().unwrap().len();
    assert!(h.service.arm(other, json!({})).await.is_err());
    assert_eq!(h.control.calls.lock().unwrap().len(), calls);
    assert_eq!(h.service.project(&id).await.unwrap()["state"], "collecting");
    h.service.stop_preparation(&id, json!({})).await.unwrap();
    assert!(h.service.authorize_control(&target, None).is_ok());
    h.close().await;
}

#[tokio::test]
async fn restart_interrupts_old_jobs_and_never_replays_without_explicit_autostart() {
    let h = Harness::new().await;
    let id = h.prepared().await;
    let mut p = h.service.project(&id).await.unwrap();
    p["state"] = json!("armed");
    p["coverage"] = json!("wide");
    p["resume_requested"] = json!(true);
    h.archive.put_monitoring("project", p).await.unwrap();
    let event = json!({"id":"old-job","project_id":id,"created_at":fixture()["clock"],"analysis":{"state":"queued"}});
    h.archive.put_monitoring("event", event).await.unwrap();
    h.service.recover_projects().await.unwrap();
    let p = h.service.project(&id).await.unwrap();
    assert_eq!(p["state"], "interrupted");
    assert_eq!(p["resume_requested"], false);
    assert_eq!(
        h.archive
            .monitoring("event", "old-job")
            .await
            .unwrap()
            .unwrap()["analysis"]["state"],
        "interrupted"
    );
    assert!(h.control.calls.lock().unwrap().is_empty());
    h.close().await;
}

#[test]
fn original_ar_dv1_rounding_and_range_are_preserved() {
    assert_eq!(
        GuardService::receiver_target(&json!({"center_frequency_hz":145500005})).unwrap(),
        145500010
    );
    assert_eq!(
        GuardService::receiver_target(&json!({"center_frequency_hz":100000})).unwrap(),
        100000
    );
    assert!(GuardService::receiver_target(&json!({"center_frequency_hz":99999})).is_err());
    assert!(GuardService::receiver_target(&json!({"center_frequency_hz":1300000001})).is_err());
}

#[tokio::test]
async fn own_preparation_can_confirm_baseline_while_collector_keeps_its_claims() {
    let h = Harness::new().await;
    let id = h.prepared().await;
    let collection = h.service.start_preparation(&id, json!({})).await.unwrap();
    let p = &collection["project"];
    let mut sweeps = fixture()["sweeps"].as_array().unwrap()[..20].to_vec();
    for s in &mut sweeps {
        s["id"] = json!(format!("own-preparation/{}", s["id"].as_str().unwrap()));
        s["session_id"] = p["active_session_id"].clone();
        s["stream_id"] = p["analyzer_stream_id"].clone();
    }
    h.archive.append_sweeps(sweeps).await.unwrap();
    let baseline = h
        .service
        .create_baseline(
            &id,
            json!({"name":"New quiet","location":"Room","confirm":true}),
        )
        .await
        .unwrap();
    assert_eq!(
        baseline["source_refs"][0]["session_id"],
        p["active_session_id"]
    );
    let target = ControlTarget {
        session_id: p["active_session_id"].as_str().unwrap().into(),
        stream_id: p["analyzer_stream_id"].as_str().unwrap().into(),
    };
    assert!(h.service.authorize_control(&target, None).is_err());
    assert_eq!(h.service.project(&id).await.unwrap()["state"], "ready");
    h.service.stop_preparation(&id, json!({})).await.unwrap();
    h.close().await;
}

#[tokio::test]
async fn startup_awaits_opt_in_preflight_and_storage_failure_stops_even_manual_roles() {
    for known in [true, false] {
        let h = Harness::new().await;
        let id = h.prepared().await;
        h.service
            .update_settings(&id, json!({"autostart":true}))
            .await
            .unwrap();
        let mut p = h.service.project(&id).await.unwrap();
        p["state"] = json!("armed");
        p["resume_requested"] = json!(true);
        h.archive.put_monitoring("project", p).await.unwrap();
        *h.control.ready.lock().unwrap() = if known { Some(true) } else { None };
        h.service.start().await.unwrap();
        assert_eq!(h.service.startup_status().await["recovery_complete"], true);
        if known {
            let p = h.service.project(&id).await.unwrap();
            assert_eq!(p["state"], "armed");
            assert_eq!(p["resume_requested"], false);
            let calls = h.control.calls.lock().unwrap().len();
            h.service.start().await.unwrap();
            assert_eq!(h.control.calls.lock().unwrap().len(), calls);
            h.service
                .manual_acquire(&id, json!({"role":"analyzer"}))
                .await
                .unwrap();
            h.service
                .manual_acquire(&id, json!({"role":"receiver"}))
                .await
                .unwrap();
            h.control.storage.lock().unwrap().writable = Some(false);
            h.sources.advance(3);
            tokio::time::timeout(std::time::Duration::from_secs(3), async {
                loop {
                    let status = h.service.guard_status(&id).await.unwrap();
                    if status["project"]["state"] == "error" {
                        assert_eq!(status["active"], false);
                        let workers = h.control.workers.lock().unwrap();
                        let owned = workers
                            .iter()
                            .filter(|w| w.session_id == p["active_session_id"].as_str().unwrap())
                            .collect::<Vec<_>>();
                        if owned.len() == 2 && owned.iter().all(|w| !w.alive) {
                            break;
                        }
                    }
                    tokio::task::yield_now().await;
                }
            })
            .await
            .unwrap();
            assert!(
                h.service.any_armed(),
                "failed runtime retains claims until proved cleanup"
            );
            assert!(
                h.control
                    .workers
                    .lock()
                    .unwrap()
                    .iter()
                    .filter(|w| w.session_id == p["active_session_id"].as_str().unwrap())
                    .all(|w| !w.alive)
            );
        } else {
            assert_eq!(h.service.project(&id).await.unwrap()["state"], "error");
            assert!(!h.service.any_armed());
            assert!(h.control.calls.lock().unwrap().is_empty());
        }
        h.close().await;
    }
}

#[tokio::test]
async fn partial_worker_fault_keeps_other_registration_alive_and_recovery_restores_coverage() {
    let h = Harness::new().await;
    let id = h.prepared().await;
    let p = h.service.arm(&id, json!({})).await.unwrap();
    h.control.calls.lock().unwrap().clear();
    {
        let mut workers = h.control.workers.lock().unwrap();
        let receiver = workers
            .iter_mut()
            .find(|w| w.stream_id == p["receiver_stream_id"].as_str().unwrap())
            .unwrap();
        receiver.alive = false;
        receiver.state = "error".into();
    }
    h.service.tick(&id).await.unwrap();
    assert_eq!(h.service.project(&id).await.unwrap()["state"], "degraded");
    assert_eq!(h.service.project(&id).await.unwrap()["coverage"], "wide");
    assert!(
        h.control
            .workers
            .lock()
            .unwrap()
            .iter()
            .any(|w| w.stream_id == p["analyzer_stream_id"].as_str().unwrap() && w.alive)
    );
    assert!(h.control.calls.lock().unwrap().is_empty());
    {
        let mut workers = h.control.workers.lock().unwrap();
        let receiver = workers
            .iter_mut()
            .find(|w| w.stream_id == p["receiver_stream_id"].as_str().unwrap())
            .unwrap();
        receiver.alive = true;
        receiver.state = "running".into();
    }
    h.service.tick(&id).await.unwrap();
    assert_eq!(h.service.project(&id).await.unwrap()["state"], "armed");
    {
        let mut workers = h.control.workers.lock().unwrap();
        workers
            .iter_mut()
            .find(|w| w.stream_id == p["analyzer_stream_id"].as_str().unwrap())
            .unwrap()
            .alive = false;
    }
    h.service.tick(&id).await.unwrap();
    assert_eq!(
        h.service.project(&id).await.unwrap()["coverage"],
        "unavailable"
    );
    assert!(
        h.control
            .workers
            .lock()
            .unwrap()
            .iter()
            .any(|w| w.stream_id == p["receiver_stream_id"].as_str().unwrap() && w.alive)
    );
    h.close().await;
}
