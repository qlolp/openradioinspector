//! Actual Archive lifecycle and original pending authority, without hardware.
use super::*;
use crate::security::ListenerRole;
use ori_automation::{AutomationEffects, AutomationScope};
use serde_json::json;

async fn original(core: &Arc<Core>) -> crate::security::AuthReceipt {
    core.security
        .check(
            &axum::http::Method::POST,
            "/api/v1/sessions",
            &Default::default(),
            ListenerRole::Plain,
        )
        .await
        .unwrap()
        .receipt()
}
fn session() -> Value {
    let fixture: Value = serde_json::from_str(include_str!(
        "../../../../contracts/fixtures/automation-domain-v1.json"
    ))
    .unwrap();
    fixture["cases"][0]["seed"]["sessions"][0].clone()
}

#[tokio::test]
async fn running_setup_keeps_its_actual_lifecycle_after_stop_and_restart() {
    let directory = tempfile::tempdir().unwrap();
    let core = Core::open(crate::CoreConfig::replay(directory.path()))
        .await
        .unwrap();
    let archive = core.archive.as_ref().unwrap();
    let source = session();
    let id = source["id"].as_str().unwrap().to_owned();
    archive.create_session(source.clone()).await.unwrap();
    let enrollment = archive.capture_session_lifecycle(&id).await.unwrap();
    let binding = core.automation.get().unwrap();
    let scope = AutomationScope {
        session_id: id.clone(),
        kind: PolicyKind::Completion,
        stream_id: None,
    };
    let policy = Authority::capture(
        &core,
        PolicyKind::Completion,
        &id,
        &source,
        &json!({"enabled":true,"receiver_stream_id":null}),
        authority::Admission {
            auth: original(&core).await,
            control_owner: None,
            closed: binding.closed.clone(),
            enrollment,
        },
    )
    .unwrap();
    binding.registry.lock().unwrap().policies.insert(
        Key::new(&id, PolicyKind::Completion),
        authority::Record {
            active: Some(policy),
            pending: None,
        },
    );
    let effects = effects::Effects {
        core: Arc::downgrade(&core),
        registry: binding.registry.clone(),
        closed: binding.closed.clone(),
    };
    let admitted = effects.capture(scope.clone()).unwrap();
    let running = effects.setup_running(scope, admitted).unwrap();
    running.revalidate().unwrap();
    let mut stopped = source.clone();
    stopped["state"] = json!("stopped");
    stopped["ended_at"] = json!("2026-11-11T12:00:02Z");
    archive
        .stop_session(stopped, "2026-11-11T12:00:02Z")
        .await
        .unwrap();
    let stopped_denied = running.revalidate().is_err();
    archive.save_session(source, None).await.unwrap();
    let restarted = archive.cached_session_lifecycle(&id).unwrap();
    let restart_denied = running.revalidate().is_err();
    drop(running);
    core.close().await.unwrap();
    assert!(stopped_denied);
    assert!(
        restart_denied,
        "A new Running generation cannot refresh an existing dispatch"
    );
    assert!(
        !restarted.check(),
        "Core close invalidates even a separately captured current receipt"
    );
}

#[tokio::test]
async fn queued_original_auth_revocation_changes_no_policy_or_canonical_event() {
    let directory = tempfile::tempdir().unwrap();
    let core = Core::open(crate::CoreConfig::replay(directory.path()))
        .await
        .unwrap();
    let source = session();
    let id = source["id"].as_str().unwrap().to_owned();
    let archive = core.archive.as_ref().unwrap();
    archive.create_session(source).await.unwrap();
    let binding = core.automation.get().unwrap();
    let coordinator = archive.domain_coordinator();
    let held = coordinator.lock().await;
    let configuring = binding.configure(
        PolicyKind::Completion,
        id.clone(),
        json!({"enabled":true}),
        original(&core).await,
        None,
    );
    tokio::pin!(configuring);
    assert!(
        std::future::poll_fn(|cx| std::task::Poll::Ready(
            std::future::Future::poll(configuring.as_mut(), cx).is_pending()
        ))
        .await
    );
    tokio::time::timeout(std::time::Duration::from_secs(2), async {
        loop {
            if binding
                .registry
                .lock()
                .unwrap()
                .policies
                .get(&Key::new(&id, PolicyKind::Completion))
                .is_some_and(|record| record.pending.is_some())
            {
                break;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    core.security.begin_shutdown();
    drop(held);
    let result = configuring.await;
    let status = binding
        .service
        .status(PolicyKind::Completion, &id)
        .await
        .unwrap();
    let events = archive.events(0, 100).await.unwrap();
    let pending = binding.registry.lock().unwrap().policies.len();
    core.close().await.unwrap();
    assert!(result.is_err());
    assert_eq!(status["policy"]["enabled"], false);
    assert_eq!(pending, 0);
    assert!(
        !events
            .iter()
            .any(|event| event["event_type"] == "automatic_completion.policy_changed")
    );
}

#[tokio::test]
async fn dropped_configuration_waiter_cannot_drop_owned_admission_or_its_close_join() {
    let directory = tempfile::tempdir().unwrap();
    let core = Core::open(crate::CoreConfig::replay(directory.path()))
        .await
        .unwrap();
    let source = session();
    let id = source["id"].as_str().unwrap().to_owned();
    let archive = core.archive.as_ref().unwrap();
    archive.create_session(source).await.unwrap();
    let binding = core.automation.get().unwrap();
    let coordinator = archive.domain_coordinator();
    let held = coordinator.lock().await;
    let authority = original(&core).await;
    let owned = binding.clone();
    let session_id = id.clone();
    let waiter = tokio::spawn(async move {
        owned
            .configure(
                PolicyKind::Completion,
                session_id,
                json!({"enabled":true}),
                authority,
                None,
            )
            .await
    });
    tokio::time::timeout(std::time::Duration::from_secs(2), async {
        loop {
            if binding
                .registry
                .lock()
                .unwrap()
                .policies
                .get(&Key::new(&id, PolicyKind::Completion))
                .is_some_and(|record| record.pending.is_some())
            {
                break;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    waiter.abort();
    assert!(waiter.await.unwrap_err().is_cancelled());
    core.security.begin_shutdown();
    let close = binding.begin_shutdown();
    let joining = close.completed();
    tokio::pin!(joining);
    let pending = std::future::poll_fn(|cx| {
        std::task::Poll::Ready(std::future::Future::poll(joining.as_mut(), cx).is_pending())
    })
    .await;
    let writer_owned = ori_runtime::lease::WriterLease::acquire(directory.path()).is_err();
    drop(held);
    joining.await.unwrap();
    let remaining = binding.registry.lock().unwrap().policies.len();
    let events = archive.events(0, 100).await.unwrap();
    core.close().await.unwrap();
    assert!(
        pending,
        "Owned close cannot acknowledge a configuration command still awaiting admission"
    );
    assert!(writer_owned);
    assert_eq!(
        remaining, 0,
        "The owned command must remove rejected pending authority after its caller is gone"
    );
    assert!(
        !events
            .iter()
            .any(|event| event["event_type"] == "automatic_completion.policy_changed")
    );
    drop(ori_runtime::lease::WriterLease::acquire(directory.path()).unwrap());
}

#[tokio::test]
async fn manual_return_reacquire_and_renew_never_refresh_an_old_policy_grant() {
    let directory = tempfile::tempdir().unwrap();
    let mut configuration = crate::CoreConfig::replay(directory.path());
    configuration.max_archive_bytes = Some(64 * 1024 * 1024);
    let core = Core::open(configuration).await.unwrap();
    let archive = core.archive.as_ref().unwrap();
    let mut source = session();
    source["id"] = json!("guard-source");
    let mut roles = std::collections::BTreeSet::new();
    source["streams"].as_array_mut().unwrap().retain(|stream| {
        let role = stream["role"].as_str().unwrap();
        ["spectrum_analyzer", "receiver"].contains(&role) && roles.insert(role.to_owned())
    });
    for stream in source["streams"].as_array_mut().unwrap() {
        stream["session_id"] = json!("guard-source");
        if stream["role"] == "spectrum_analyzer" {
            stream["id"] = json!("guard-spectrum");
            stream["settings"] = json!({"adapter":"tinysa-sim","start_hz":100000000,"stop_hz":100020000,"points":3,"noise_sigma_db":0,"emitters":[],"period_s":0.01});
        } else {
            stream["id"] = json!("guard-receiver");
            stream["settings"] = json!({"adapter":"ardv1-sim","period_s":0.01});
        }
    }
    archive.create_session(source).await.unwrap();
    let now = chrono::Utc::now();
    let sweeps=(0..20).map(|index| {
        let start=now-chrono::Duration::milliseconds(70000-index*3500);
        let end=start+chrono::Duration::milliseconds(10);
        json!({"id":format!("guard-baseline-{index}"),"session_id":"guard-source","stream_id":"guard-spectrum","interval":{"started_at":start.to_rfc3339_opts(chrono::SecondsFormat::Micros,true),"ended_at":end.to_rfc3339_opts(chrono::SecondsFormat::Micros,true)},"frequencies_hz":[100000000,100010000,100020000],"levels":[-100.0,-100.0,-100.0],"level_unit":"dBm","settings":{"adapter":"tinysa-sim","rbw_hz":10000,"sequence":index+1,"simulated":true,"generator_output":"off"}})
    }).collect();
    archive.append_sweeps(sweeps).await.unwrap();
    let guard = &core.domains.get().unwrap().guard;
    let project=guard.create(json!({"name":"Owned manual authority","site":"Synthetic only","session_id":"guard-source"})).await.unwrap();
    let project_id = project["id"].as_str().unwrap();
    guard
        .create_baseline(
            project_id,
            json!({"name":"Owned baseline","location":"Synthetic only","confirm":true}),
        )
        .await
        .unwrap();
    // This short authority test deliberately produces 100 Hz synthetic data.
    // Give it an explicit short recording policy, independent of host free
    // space and unrelated one-hour monitoring defaults. Production is unchanged.
    guard
        .update_settings(
            project_id,
            json!({"archive":{"minimum_free_bytes":1_048_576,"minimum_retention_seconds":60}}),
        )
        .await
        .unwrap();
    let armed = guard.arm(project_id, json!({})).await.unwrap();
    let id = armed["active_session_id"].as_str().unwrap();
    let stream = armed["receiver_stream_id"].as_str().unwrap();
    let lease = guard
        .manual_acquire(project_id, json!({"role":"receiver"}))
        .await
        .unwrap();
    let token = lease["token"].as_str().unwrap();
    let mut commits = archive.subscribe_commits();
    tokio::time::timeout(std::time::Duration::from_secs(2), async {
        while archive
            .list_sweeps(id, Some(armed["analyzer_stream_id"].as_str().unwrap()), 2)
            .await
            .unwrap()
            .len()
            < 2
        {
            assert!(!matches!(
                commits.recv().await,
                Err(tokio::sync::broadcast::error::RecvError::Closed)
            ));
        }
    })
    .await
    .unwrap();
    let budget = guard.storage_report(project_id, true).await.unwrap();
    if budget["ready"] != true {
        let evidence = json!({"bytes_per_second":budget["bytes_per_second"],"available_bytes":budget["available_bytes"],"required_retention_seconds":budget["required_retention_seconds"],"estimated_retention_seconds":budget["estimated_retention_seconds"]});
        core.close().await.unwrap();
        panic!("manual-grant fixture exhausted its unrelated storage budget: {evidence}");
    }
    let binding = core.automation.get().unwrap();
    let policy = json!({"enabled":true,"receiver_stream_id":stream});
    let configured_result = binding
        .configure(
            PolicyKind::Completion,
            id.into(),
            policy.clone(),
            original(&core).await,
            Some(token.into()),
        )
        .await;
    let configured = match configured_result {
        Ok(value) => value,
        Err(error) => {
            let project = guard.project(project_id).await.unwrap();
            let permission_error = guard
                .capture_instrument_control(
                    &ori_guard::ControlTarget {
                        session_id: id.into(),
                        stream_id: stream.into(),
                    },
                    Some(token),
                )
                .err()
                .map(|error| error.to_string());
            let evidence = json!({"error_code":error.api_code,"project_state":project["state"],"project_error":project["last_error"],"coverage":project["coverage"],"permission_error":permission_error});
            core.close().await.unwrap();
            panic!("initial manual policy configuration failed after owned cleanup: {evidence}");
        }
    };
    let old = binding
        .registry
        .lock()
        .unwrap()
        .policies
        .get(&Key::new(id, PolicyKind::Completion))
        .unwrap()
        .active
        .clone()
        .unwrap();
    let before_rejected = binding
        .service
        .status(PolicyKind::Completion, id)
        .await
        .unwrap();
    let before_events = archive
        .events(0, 1000)
        .await
        .unwrap()
        .into_iter()
        .filter(|event| event["event_type"] == "automatic_completion.policy_changed")
        .collect::<Vec<_>>();
    let missing_owner = binding
        .configure(
            PolicyKind::Completion,
            id.into(),
            json!({"enabled":false}),
            original(&core).await,
            None,
        )
        .await;
    let after_rejected = binding
        .service
        .status(PolicyKind::Completion, id)
        .await
        .unwrap();
    let after_events = archive
        .events(0, 1000)
        .await
        .unwrap()
        .into_iter()
        .filter(|event| event["event_type"] == "automatic_completion.policy_changed")
        .collect::<Vec<_>>();
    guard
        .manual_renew(project_id, json!({"token":token}))
        .await
        .unwrap();
    let renewed_valid = old.revalidate().is_ok();
    guard
        .manual_return(project_id, json!({"token":token}))
        .await
        .unwrap();
    let returned_denied = old.revalidate().is_err();
    let second = guard
        .manual_acquire(project_id, json!({"role":"receiver"}))
        .await
        .unwrap();
    let second_token = second["token"].as_str().unwrap();
    let reacquired_denied = old.revalidate().is_err();
    let original_capture_denied = effects::Effects {
        core: Arc::downgrade(&core),
        registry: binding.registry.clone(),
        closed: binding.closed.clone(),
    }
    .capture(AutomationScope {
        session_id: id.into(),
        kind: PolicyKind::Completion,
        stream_id: Some(stream.into()),
    })
    .is_err();
    let original_dispatch = binding.service.run_once(PolicyKind::Completion, id).await;
    let explicit = binding
        .configure(
            PolicyKind::Completion,
            id.into(),
            policy,
            original(&core).await,
            Some(second_token.into()),
        )
        .await;
    let still_denied = old.revalidate().is_err();
    let events = archive.events(0, 1000).await.unwrap();
    core.close().await.unwrap();
    assert_eq!(configured["policy"]["enabled"], true);
    assert!(
        missing_owner.is_err(),
        "A missing manual token cannot mutate or revoke an admitted claimed policy"
    );
    assert_eq!(after_rejected["policy"], before_rejected["policy"]);
    assert_eq!(after_events, before_events);
    assert!(
        renewed_valid,
        "An explicit legitimate renewal keeps the SAME original grant valid"
    );
    assert!(returned_denied && reacquired_denied && still_denied);
    assert!(
        original_capture_denied,
        "Dispatch capture must retain the admitted old grant, not look up a replacement token"
    );
    assert!(
        original_dispatch.is_err(),
        "A scheduled pass cannot recapture the replacement manual token"
    );
    assert!(
        explicit.is_ok(),
        "A new explicit update may admit its own new token"
    );
    let encoded = ori_contracts::to_json_vec(&events).unwrap();
    for secret in [token, second_token] {
        assert!(
            !encoded
                .windows(secret.len())
                .any(|window| window == secret.as_bytes())
        );
    }
}
