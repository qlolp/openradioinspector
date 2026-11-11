use ori_control::*;
use serde_json::{Value, json};
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, AtomicU64, Ordering},
};
use tokio::sync::Semaphore;
struct Authority {
    target: Target,
    live: Arc<AtomicBool>,
}
impl OwnerAuthority for Authority {
    fn target(&self) -> &Target {
        &self.target
    }
    fn worker_instance(&self) -> &str {
        "owned-worker"
    }
    fn connection_id(&self) -> &str {
        "owned-connection"
    }
    fn revalidate(&self) -> ori_control::Result<()> {
        if self.live.load(Ordering::SeqCst) {
            Ok(())
        } else {
            Err(ControlError::Denied)
        }
    }
}
struct Executor {
    live: Arc<AtomicBool>,
    revision: AtomicU64,
    entered: Semaphore,
    release: Semaphore,
    calls: Mutex<Vec<OperationKind>>,
    hold: AtomicBool,
    cancelled: AtomicBool,
    proof: AtomicBool,
    guard: AtomicBool,
}
impl Executor {
    fn owner(&self, t: &Target) -> OwnerReceipt {
        OwnerReceipt::from_backend(Arc::new(Authority {
            target: t.clone(),
            live: self.live.clone(),
        }))
        .unwrap()
    }
    fn snapshot_for(&self, t: &Target) -> CachedInstrument {
        CachedInstrument {
            target: t.clone(),
            family: InstrumentFamily::TinySa,
            status: json!({"mode":"monitoring","revision":self.revision.load(Ordering::SeqCst),"connection_id":"owned-connection","settings":{},"device_mode":"input"}),
            control_revision: self.revision.load(Ordering::SeqCst),
            owner: Some(self.owner(t)),
            supported_operations: OperationKind::ALL.iter().copied().collect(),
            guard_claimed: self.guard.load(Ordering::SeqCst),
            coverage_bridge_available: false,
            science_available: false,
            audio_available: false,
        }
    }
}
impl ControlExecutor for Executor {
    fn snapshot(&self, t: &Target) -> ori_control::Result<CachedInstrument> {
        Ok(self.snapshot_for(t))
    }
    fn execute<'a>(&'a self, r: ExecutionRequest) -> ControlFuture<'a, ExecutionOutcome> {
        Box::pin(async move {
            let execution = BoundExecution::bind(
                r.owner,
                r.expected_revision,
                self.revision.load(Ordering::SeqCst),
            )?;
            (r.check)().map_err(|_| ControlError::Denied)?;
            self.calls.lock().unwrap().push(r.operation.kind());
            self.entered.add_permits(1);
            if self.hold.load(Ordering::SeqCst) {
                self.release.acquire().await.unwrap().forget();
            }
            (r.check)()
                .map_err(|_| ControlError::Indeterminate("owned exchange interrupted".into()))?;
            let revision = self.revision.fetch_add(1, Ordering::SeqCst) + 1;
            let at = chrono::Utc::now();
            Ok(ExecutionOutcome {
                execution,
                applied_revision: revision,
                status: json!({"mode":if matches!(r.operation,Operation::LeaseAcquire{..}){"lab"}else{"monitoring"},"revision":revision,"connection_id":"owned-connection","settings":{},"device_mode":"input"}),
                steps: if matches!(r.operation, Operation::Capture { .. }) {
                    vec![ExecutedStep {
                        reply: AdapterReply::TinySa(ori_instruments::TinySaReply::Screen(
                            b"\xf8\0".repeat(480 * 320),
                        )),
                        interval: ori_instruments::OperationInterval {
                            started_at: at,
                            ended_at: at,
                        },
                    }]
                } else {
                    vec![]
                },
                interval: ori_instruments::OperationInterval {
                    started_at: at,
                    ended_at: at,
                },
            })
        })
    }
    fn cancel_and_join<'a>(
        &'a self,
        _r: CancellationRequest,
    ) -> ControlFuture<'a, CancellationOutcome> {
        Box::pin(async move {
            self.cancelled.store(true, Ordering::SeqCst);
            self.release.add_permits(100);
            Ok(CancellationOutcome {
                original_worker_joined: self.proof.load(Ordering::SeqCst),
                original_transport_dropped: self.proof.load(Ordering::SeqCst),
                tainted: true,
            })
        })
    }
}
struct Store {
    events: Mutex<Vec<(OperationState, Value)>>,
    hold_terminal: AtomicBool,
    terminal_entered: Semaphore,
    release: Semaphore,
    fail: AtomicBool,
}
impl ControlCommitter for Store {
    fn commit<'a>(&'a self, b: ControlCommit) -> ControlFuture<'a, CommitOutcome> {
        Box::pin(async move {
            if b.event.state.terminal() && self.hold_terminal.load(Ordering::SeqCst) {
                self.terminal_entered.add_permits(1);
                self.release.acquire().await.unwrap().forget();
            }
            if self.fail.load(Ordering::SeqCst) {
                return Err(ControlError::Persistence("owned failure".into()));
            }
            self.events
                .lock()
                .unwrap()
                .push((b.event.state, b.event.fields));
            Ok(CommitOutcome {
                event_sequence: self.events.lock().unwrap().len() as u64,
                artifact_summary: b.artifact.map(|a| a.summary()),
            })
        })
    }
}
fn setup(config: ControlConfig) -> (Coordinator, Arc<Executor>, Arc<Store>, Target) {
    let e = Arc::new(Executor {
        live: Arc::new(AtomicBool::new(true)),
        revision: AtomicU64::new(0),
        entered: Semaphore::new(0),
        release: Semaphore::new(0),
        calls: Mutex::new(vec![]),
        hold: AtomicBool::new(false),
        cancelled: AtomicBool::new(false),
        proof: AtomicBool::new(true),
        guard: AtomicBool::new(false),
    });
    let s = Arc::new(Store {
        events: Mutex::new(vec![]),
        hold_terminal: AtomicBool::new(false),
        terminal_entered: Semaphore::new(0),
        release: Semaphore::new(0),
        fail: AtomicBool::new(false),
    });
    (
        Coordinator::new(e.clone(), s.clone(), config).unwrap(),
        e,
        s,
        Target {
            session_id: "owned-session".into(),
            stream_id: "owned-stream".into(),
        },
    )
}
async fn done(c: &Coordinator, t: &Target, id: &str, client: Option<&str>) -> Value {
    tokio::time::timeout(std::time::Duration::from_secs(3), async {
        loop {
            let v = c.get(t, id, client).unwrap();
            if matches!(
                v["state"].as_str(),
                Some("succeeded" | "failed" | "cancelled" | "unknown")
            ) {
                return v;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap()
}
#[tokio::test]
async fn terminal_view_waits_for_commit_and_duplicate_never_replays_io() {
    let (c, e, s, t) = setup(ControlConfig::default());
    s.hold_terminal.store(true, Ordering::SeqCst);
    let raw =
        br#"{"operation":"setting","parameters":{"name":"repeat","value":3},"request_id":"one"}"#;
    let submitted = c.submit(&t, raw, None).await.unwrap();
    assert!(submitted.get("result").is_none());
    let id = submitted["operation_id"].as_str().unwrap();
    s.terminal_entered.acquire().await.unwrap().forget();
    assert_eq!(c.get(&t, id, None).unwrap()["state"], "running");
    assert_eq!(c.get(&t, id, None).unwrap()["result"], Value::Null);
    assert_eq!(c.submit(&t, raw, None).await.unwrap()["operation_id"], id);
    assert_eq!(e.calls.lock().unwrap().len(), 1);
    s.release.add_permits(1);
    assert_eq!(done(&c, &t, id, None).await["state"], "succeeded");
    c.close().await.unwrap();
}
#[tokio::test]
async fn omitted_revisions_queue_but_explicit_stale_fails_and_old_authority_never_refreshes() {
    let (c, e, _, t) = setup(ControlConfig::default());
    e.hold.store(true, Ordering::SeqCst);
    let one = c
        .submit(&t, br#"{"operation":"output.off"}"#, None)
        .await
        .unwrap();
    e.entered.acquire().await.unwrap().forget();
    let two = c
        .submit(
            &t,
            br#"{"operation":"setting","parameters":{"name":"repeat","value":2}}"#,
            None,
        )
        .await
        .unwrap();
    e.release.add_permits(2);
    assert_eq!(
        done(&c, &t, one["operation_id"].as_str().unwrap(), None).await["state"],
        "succeeded"
    );
    assert_eq!(
        done(&c, &t, two["operation_id"].as_str().unwrap(), None).await["state"],
        "succeeded"
    );
    e.hold.store(false, Ordering::SeqCst);
    let stale=c.submit(&t,br#"{"operation":"setting","expected_revision":0,"parameters":{"name":"repeat","value":3}}"#,None).await.unwrap();
    assert_eq!(
        done(&c, &t, stale["operation_id"].as_str().unwrap(), None).await["state"],
        "failed"
    );
    assert_eq!(e.calls.lock().unwrap().len(), 2);
    c.close().await.unwrap();
}
#[tokio::test]
async fn reserved_off_and_other_instrument_are_admitted_under_normal_queue_pressure() {
    let config = ControlConfig {
        max_normal_per_instrument: 1,
        ..ControlConfig::default()
    };
    let (c, e, _, t) = setup(config);
    e.hold.store(true, Ordering::SeqCst);
    let first = c
        .submit(&t, br#"{"operation":"inspect"}"#, None)
        .await
        .unwrap();
    e.entered.acquire().await.unwrap().forget();
    assert!(
        c.submit(&t, br#"{"operation":"setting","parameters":{}}"#, None)
            .await
            .is_err()
    );
    let off = c
        .submit(&t, br#"{"operation":"output.off"}"#, None)
        .await
        .unwrap();
    e.entered.acquire().await.unwrap().forget();
    assert_eq!(
        c.submit(&t, br#"{"operation":"output.off"}"#, None)
            .await
            .unwrap()["operation_id"],
        off["operation_id"]
    );
    let other = Target {
        session_id: t.session_id.clone(),
        stream_id: "other".into(),
    };
    let second = c
        .submit(&other, br#"{"operation":"inspect"}"#, None)
        .await
        .unwrap();
    e.entered.acquire().await.unwrap().forget();
    e.release.add_permits(3);
    for (target, id) in [
        (&t, first["operation_id"].as_str().unwrap()),
        (&t, off["operation_id"].as_str().unwrap()),
        (&other, second["operation_id"].as_str().unwrap()),
    ] {
        done(&c, target, id, None).await;
    }
    c.close().await.unwrap();
}
#[tokio::test]
async fn lease_secret_only_reaches_its_owner_and_renewal_does_not_wait_for_io() {
    let (c, e, s, t) = setup(ControlConfig::default());
    let client = "owned-client-0123456789";
    let acquired = c
        .submit(
            &t,
            br#"{"operation":"lease.acquire","parameters":{"acknowledge_rf":true}}"#,
            Some(client),
        )
        .await
        .unwrap();
    let id = acquired["operation_id"].as_str().unwrap();
    let view = done(&c, &t, id, Some(client)).await;
    let secret = view["result"]["lease_id"].as_str().unwrap();
    assert!(
        c.get(&t, id, None).unwrap()["result"]
            .get("lease_id")
            .is_none()
    );
    assert!(!format!("{:?}", c.list(&t).unwrap()).contains(secret));
    assert!(!format!("{:?}", s.events.lock().unwrap()).contains(secret));
    e.hold.store(true, Ordering::SeqCst);
    let blocked=c.submit(&t,format!(r#"{{"operation":"setting","parameters":{{"name":"repeat","value":2}},"lease_id":"{secret}"}}"#).as_bytes(),Some(client)).await.unwrap();
    e.entered.acquire_many(2).await.unwrap().forget();
    let renew = c
        .submit(
            &t,
            format!(r#"{{"operation":"lease.renew","lease_id":"{secret}","expected_revision":0}}"#)
                .as_bytes(),
            Some(client),
        )
        .await
        .unwrap();
    assert_eq!(
        done(
            &c,
            &t,
            renew["operation_id"].as_str().unwrap(),
            Some(client)
        )
        .await["state"],
        "succeeded"
    );
    e.release.add_permits(1);
    done(
        &c,
        &t,
        blocked["operation_id"].as_str().unwrap(),
        Some(client),
    )
    .await;
    c.close().await.unwrap();
}
#[tokio::test]
async fn terminal_persistence_failure_is_sticky_and_never_replays_hardware() {
    let (c, e, s, t) = setup(ControlConfig::default());
    s.hold_terminal.store(true, Ordering::SeqCst);
    let job = c
        .submit(&t, br#"{"operation":"inspect","request_id":"safe"}"#, None)
        .await
        .unwrap();
    s.terminal_entered.acquire().await.unwrap().forget();
    s.fail.store(true, Ordering::SeqCst);
    s.release.add_permits(1);
    tokio::task::yield_now().await;
    assert_eq!(
        c.get(&t, job["operation_id"].as_str().unwrap(), None)
            .unwrap()["state"],
        "running"
    );
    assert!(c.close().await.is_err());
    assert!(c.close().await.is_err());
    assert_eq!(e.calls.lock().unwrap().len(), 1);
}

#[tokio::test]
async fn closing_even_idle_admitted_context_joins_its_original_transport_and_latches_missing_proof()
{
    let (c, e, _, t) = setup(ControlConfig::default());
    let submitted = c
        .submit(
            &t,
            br#"{"operation":"setting","parameters":{"name":"repeat","value":2}}"#,
            None,
        )
        .await
        .unwrap();
    assert_eq!(
        done(&c, &t, submitted["operation_id"].as_str().unwrap(), None).await["state"],
        "succeeded"
    );
    e.proof.store(false, Ordering::SeqCst);
    assert!(c.close().await.is_err());
    assert!(e.cancelled.load(Ordering::SeqCst));
    e.proof.store(true, Ordering::SeqCst);
    assert!(c.close().await.is_err());
}

#[tokio::test]
async fn ledger_survives_history_eviction_and_fingerprint_collisions_never_replay() {
    let config = ControlConfig {
        max_history: 2,
        max_idempotency: 2,
        ..ControlConfig::default()
    };
    let (c, e, _, t) = setup(config);
    for id in ["one", "two"] {
        let request = format!(
            r#"{{"operation":"setting","parameters":{{"name":"repeat","value":2}},"request_id":"{id}"}}"#
        );
        let job = c.submit(&t, request.as_bytes(), None).await.unwrap();
        assert_eq!(
            done(&c, &t, job["operation_id"].as_str().unwrap(), None).await["state"],
            "succeeded"
        );
        tokio::task::yield_now().await;
    }
    assert!(c.submit(&t,br#"{"operation":"setting","parameters":{"name":"repeat","value":3},"request_id":"one"}"#,None).await.is_err());
    assert!(c.submit(&t,br#"{"operation":"setting","parameters":{"name":"repeat","value":2},"request_id":"three"}"#,None).await.is_err());
    let third = c
        .submit(
            &t,
            br#"{"operation":"setting","parameters":{"name":"repeat","value":2}}"#,
            None,
        )
        .await
        .unwrap();
    done(&c, &t, third["operation_id"].as_str().unwrap(), None).await;
    tokio::task::yield_now().await;
    assert!(c.submit(&t,br#"{"operation":"setting","parameters":{"name":"repeat","value":2},"request_id":"one"}"#,None).await.is_err());
    assert_eq!(e.calls.lock().unwrap().len(), 3);
    // A full mutation ledger is permanent, but neither reserved safe-off nor
    // lease heartbeat consumes another mutation slot.
    let client = "owned-ledger-pressure-0123456789";
    let acquired = c
        .submit(
            &t,
            br#"{"operation":"lease.acquire","parameters":{"acknowledge_rf":true}}"#,
            Some(client),
        )
        .await
        .unwrap();
    let acquired = done(
        &c,
        &t,
        acquired["operation_id"].as_str().unwrap(),
        Some(client),
    )
    .await;
    let secret = acquired["result"]["lease_id"].as_str().unwrap();
    let renewed = c.submit(&t, format!(r#"{{"operation":"lease.renew","lease_id":"{secret}","request_id":"renew-after-full"}}"#).as_bytes(), Some(client)).await.unwrap();
    assert_eq!(
        done(
            &c,
            &t,
            renewed["operation_id"].as_str().unwrap(),
            Some(client)
        )
        .await["state"],
        "succeeded"
    );
    let off = c
        .submit(
            &t,
            br#"{"operation":"output.off","request_id":"off-after-full"}"#,
            None,
        )
        .await
        .unwrap();
    assert_eq!(
        done(&c, &t, off["operation_id"].as_str().unwrap(), None).await["state"],
        "succeeded"
    );
    c.close().await.unwrap();
}

struct HttpAuthority {
    original: OwnerReceipt,
    credential_live: Arc<AtomicBool>,
}
impl OwnerAuthority for HttpAuthority {
    fn target(&self) -> &Target {
        self.original.target()
    }
    fn worker_instance(&self) -> &str {
        self.original.worker_instance()
    }
    fn connection_id(&self) -> &str {
        self.original.connection_id()
    }
    fn revalidate(&self) -> ori_control::Result<()> {
        self.original.revalidate()?;
        if self.credential_live.load(Ordering::SeqCst) {
            Ok(())
        } else {
            Err(ControlError::Backend(
                "NEVER EXPOSE original credential token".into(),
            ))
        }
    }
}
#[tokio::test]
async fn production_override_retains_original_lan_credential_revocation_while_queued() {
    let (c, e, _, t) = setup(ControlConfig::default());
    e.hold.store(true, Ordering::SeqCst);
    let one = c
        .submit(
            &t,
            br#"{"operation":"setting","parameters":{"name":"repeat","value":2}}"#,
            None,
        )
        .await
        .unwrap();
    e.entered.acquire().await.unwrap().forget();
    let credential = Arc::new(AtomicBool::new(true));
    let mut admitted = e.snapshot_for(&t);
    admitted.owner = Some(
        OwnerReceipt::from_backend(Arc::new(HttpAuthority {
            original: admitted.owner.take().unwrap(),
            credential_live: credential.clone(),
        }))
        .unwrap(),
    );
    let two = c
        .submit_admitted(
            admitted,
            br#"{"operation":"setting","parameters":{"name":"repeat","value":3}}"#,
            None,
        )
        .await
        .unwrap();
    credential.store(false, Ordering::SeqCst);
    e.release.add_permits(1);
    done(&c, &t, one["operation_id"].as_str().unwrap(), None).await;
    let denied = done(&c, &t, two["operation_id"].as_str().unwrap(), None).await;
    assert_eq!(denied["state"], "failed");
    assert!(!denied.to_string().contains("NEVER EXPOSE"));
    assert_eq!(e.calls.lock().unwrap().len(), 1);
    assert!(e.live.load(Ordering::SeqCst));
    c.close().await.unwrap();
}

#[tokio::test]
async fn guard_claim_after_an_earlier_unclaimed_operation_does_not_reuse_stale_capabilities() {
    let (c, e, _, t) = setup(ControlConfig::default());
    let first = c
        .submit(
            &t,
            br#"{"operation":"setting","parameters":{"name":"repeat","value":2}}"#,
            None,
        )
        .await
        .unwrap();
    done(&c, &t, first["operation_id"].as_str().unwrap(), None).await;
    e.guard.store(true, Ordering::SeqCst);
    let acquire = c
        .submit(
            &t,
            br#"{"operation":"lease.acquire","parameters":{"acknowledge_rf":true}}"#,
            Some("owned-client-0123456789"),
        )
        .await
        .unwrap();
    let denied = done(&c, &t, acquire["operation_id"].as_str().unwrap(), None).await;
    assert_eq!(denied["state"], "failed");
    assert!(denied["error"].as_str().unwrap().contains("guard_manual"));
    assert_eq!(e.calls.lock().unwrap().len(), 1);
    c.close().await.unwrap();
}

#[tokio::test(start_paused = true)]
async fn lease_expiry_interrupts_the_original_execution_and_requests_only_safe_off() {
    let (c, e, _, t) = setup(ControlConfig::default());
    let client = "owned-client-0123456789";
    let acquire = c
        .submit(
            &t,
            br#"{"operation":"lease.acquire","parameters":{"acknowledge_rf":true}}"#,
            Some(client),
        )
        .await
        .unwrap();
    let view = done(
        &c,
        &t,
        acquire["operation_id"].as_str().unwrap(),
        Some(client),
    )
    .await;
    let secret = view["result"]["lease_id"].as_str().unwrap();
    e.entered.acquire().await.unwrap().forget();
    e.hold.store(true, Ordering::SeqCst);
    let setting=c.submit(&t,format!(r#"{{"operation":"setting","parameters":{{"name":"repeat","value":2}},"lease_id":"{secret}"}}"#).as_bytes(),Some(client)).await.unwrap();
    e.entered.acquire().await.unwrap().forget();
    tokio::time::advance(std::time::Duration::from_secs(31)).await;
    e.hold.store(false, Ordering::SeqCst);
    e.release.add_permits(1);
    let unknown = done(
        &c,
        &t,
        setting["operation_id"].as_str().unwrap(),
        Some(client),
    )
    .await;
    assert_eq!(unknown["state"], "unknown");
    assert_eq!(c.status(&t).unwrap()["lease_active"], false);
    tokio::time::advance(std::time::Duration::from_millis(101)).await;
    tokio::task::yield_now().await;
    assert!(e.calls.lock().unwrap().iter().all(|kind| matches!(
        kind,
        OperationKind::LeaseAcquire | OperationKind::Setting | OperationKind::OutputOff
    )));
    c.close().await.unwrap();
}

#[tokio::test]
async fn normal_input_result_byte_pressure_cannot_starve_lease_heartbeat_or_reserved_off() {
    let config = ControlConfig {
        max_instruments: 2,
        max_retained_bytes: 512 * 1024,
        ..ControlConfig::default()
    };
    let (c, e, _, t) = setup(config);
    let client = "owned-client-0123456789";
    let acquired = c
        .submit(
            &t,
            br#"{"operation":"lease.acquire","parameters":{"acknowledge_rf":true}}"#,
            Some(client),
        )
        .await
        .unwrap();
    let acquired = done(
        &c,
        &t,
        acquired["operation_id"].as_str().unwrap(),
        Some(client),
    )
    .await;
    let secret = acquired["result"]["lease_id"].as_str().unwrap();
    e.entered.acquire().await.unwrap().forget();
    e.hold.store(true, Ordering::SeqCst);
    let request = json!({"operation":"setting","parameters":{"name":"repeat","value":2,"ignored_legacy_extension":"x".repeat(60000)},"lease_id":secret});
    let raw = ori_contracts::to_json_vec(&request).unwrap();
    let mut accepted = 0;
    for _ in 0..4 {
        match c.submit(&t, &raw, Some(client)).await {
            Ok(_) => accepted += 1,
            Err(ControlError::Conflict(_)) => break,
            Err(e) => panic!("{e}"),
        }
    }
    assert!(accepted > 0);
    let renewed = c
        .submit(
            &t,
            format!(r#"{{"operation":"lease.renew","lease_id":"{secret}"}}"#).as_bytes(),
            Some(client),
        )
        .await
        .unwrap();
    assert_eq!(
        done(
            &c,
            &t,
            renewed["operation_id"].as_str().unwrap(),
            Some(client)
        )
        .await["state"],
        "succeeded"
    );
    let off = c
        .submit(&t, br#"{"operation":"output.off"}"#, None)
        .await
        .unwrap();
    assert!(off.get("result").is_none());
    e.hold.store(false, Ordering::SeqCst);
    e.release.add_permits(100);
    done(&c, &t, off["operation_id"].as_str().unwrap(), None).await;
    c.close().await.unwrap();
}

#[tokio::test]
async fn a_new_valid_http_request_cannot_refresh_a_lease_granted_to_a_revoked_credential() {
    let (c, e, _, t) = setup(ControlConfig::default());
    let credential = Arc::new(AtomicBool::new(true));
    let mut snapshot = e.snapshot_for(&t);
    snapshot.owner = Some(
        OwnerReceipt::from_backend(Arc::new(HttpAuthority {
            original: snapshot.owner.take().unwrap(),
            credential_live: credential.clone(),
        }))
        .unwrap(),
    );
    let client = "owned-client-0123456789";
    let acquire = c
        .submit_admitted(
            snapshot,
            br#"{"operation":"lease.acquire","parameters":{"acknowledge_rf":true}}"#,
            Some(client),
        )
        .await
        .unwrap();
    let view = done(
        &c,
        &t,
        acquire["operation_id"].as_str().unwrap(),
        Some(client),
    )
    .await;
    let secret = view["result"]["lease_id"].as_str().unwrap();
    credential.store(false, Ordering::SeqCst);
    let setting=c.submit(&t,format!(r#"{{"operation":"setting","parameters":{{"name":"repeat","value":2}},"lease_id":"{secret}"}}"#).as_bytes(),Some(client)).await.unwrap();
    assert_eq!(
        done(
            &c,
            &t,
            setting["operation_id"].as_str().unwrap(),
            Some(client)
        )
        .await["state"],
        "failed"
    );
    assert_eq!(e.calls.lock().unwrap().len(), 1);
    c.close().await.unwrap();
}

#[tokio::test]
async fn queued_cancel_returns_only_its_committed_cancelled_view_and_never_stops_other_io() {
    let (c, e, _, t) = setup(ControlConfig::default());
    e.hold.store(true, Ordering::SeqCst);
    let raw = br#"{"operation":"setting","parameters":{"name":"repeat","value":2}}"#;
    let first = c.submit(&t, raw, None).await.unwrap();
    e.entered.acquire().await.unwrap().forget();
    let queued = c.submit(&t, raw, None).await.unwrap();
    let cancelled = c
        .cancel(&t, queued["operation_id"].as_str().unwrap())
        .await
        .unwrap();
    assert_eq!(cancelled["state"], "cancelled");
    assert!(!e.cancelled.load(Ordering::SeqCst));
    assert_eq!(e.calls.lock().unwrap().len(), 1);
    e.release.add_permits(1);
    done(&c, &t, first["operation_id"].as_str().unwrap(), None).await;
    c.close().await.unwrap();
}

#[tokio::test]
async fn blob_reservation_is_held_through_terminal_commit_and_does_not_block_emergency_off() {
    let config = ControlConfig {
        max_blob_bytes: 1024 * 1024,
        max_inflight_blob_bytes: 1024 * 1024,
        ..ControlConfig::default()
    };
    let (c, e, s, t) = setup(config);
    s.hold_terminal.store(true, Ordering::SeqCst);
    let first = c
        .submit(
            &t,
            br#"{"operation":"capture","parameters":{"save":true}}"#,
            None,
        )
        .await
        .unwrap();
    s.terminal_entered.acquire().await.unwrap().forget();
    let other = Target {
        session_id: t.session_id.clone(),
        stream_id: "independent".into(),
    };
    let second = c
        .submit(
            &other,
            br#"{"operation":"capture","parameters":{"save":true}}"#,
            None,
        )
        .await
        .unwrap();
    loop {
        if c.get(&other, second["operation_id"].as_str().unwrap(), None)
            .unwrap()["state"]
            == "running"
        {
            break;
        }
        tokio::task::yield_now().await;
    }
    assert_eq!(
        e.calls.lock().unwrap().as_slice(),
        &[OperationKind::Capture]
    );
    let off = c
        .submit(&other, br#"{"operation":"output.off"}"#, None)
        .await
        .unwrap();
    s.terminal_entered.acquire().await.unwrap().forget();
    assert_eq!(
        e.calls.lock().unwrap().as_slice(),
        &[OperationKind::Capture, OperationKind::OutputOff]
    );
    s.hold_terminal.store(false, Ordering::SeqCst);
    s.release.add_permits(3);
    for (target, job) in [(&t, first), (&other, second), (&other, off)] {
        assert_eq!(
            done(&c, target, job["operation_id"].as_str().unwrap(), None).await["state"],
            "succeeded"
        );
    }
    c.close().await.unwrap();
}

#[tokio::test]
async fn unavailable_science_audio_and_receiver_commands_are_honest_before_any_executor_io() {
    let (c, e, _, t) = setup(ControlConfig::default());
    let caps = c.capabilities(&t).unwrap();
    assert_eq!(
        caps["capabilities"]["operations"]["measurement"]["available"],
        false
    );
    assert_eq!(
        caps["capabilities"]["operations"]["audio.capture"]["available"],
        false
    );
    for operation in ["measurement", "audio.inputs"] {
        let raw = ori_contracts::to_json_vec(&json!({"operation":operation})).unwrap();
        let job = c.submit(&t, &raw, None).await.unwrap();
        let view = done(&c, &t, job["operation_id"].as_str().unwrap(), None).await;
        assert_eq!(view["state"], "failed");
        assert!(view["error"].as_str().unwrap().contains("unavailable"));
    }
    let mut receiver = e.snapshot_for(&t);
    receiver.family = InstrumentFamily::ArDv1;
    assert!(matches!(
        c.submit_admitted(
            receiver,
            br#"{"operation":"keypad","parameters":{"text":"1"}}"#,
            None
        )
        .await,
        Err(ControlError::Unavailable(_))
    ));
    assert!(e.calls.lock().unwrap().is_empty());
    c.close().await.unwrap();
}
