use chrono::{DateTime, TimeDelta, Utc};
use ori_archive::{Archive, ArchiveConfig};
use ori_automation::{
    AutomationConfig, AutomationEffects, AutomationScope, AutomationService, DetectedRegion,
    DetectionRequest, PolicyKind,
};
use ori_runtime::worker::Cancellation;
use ori_session::{
    SessionEffects, SessionError, SessionFuture, SessionService, TuneCompletionCheck, TuneReceipt,
    TuneRequest,
};
use serde_json::{Value, json};
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicUsize, Ordering},
};

struct Check;
impl TuneCompletionCheck for Check {
    fn revalidate(&self) -> Result<(), SessionError> {
        Ok(())
    }
}
struct Effects {
    now: Arc<Mutex<DateTime<Utc>>>,
    tick: Arc<Mutex<f64>>,
    failure: Mutex<Option<String>>,
    delay: Mutex<i64>,
}
impl Effects {
    fn tune_impl(&self, check: Arc<dyn TuneCompletionCheck>) -> SessionFuture<'_, TuneReceipt> {
        Box::pin(async move {
            check.revalidate()?;
            let delay = *self.delay.lock().unwrap();
            *self.now.lock().unwrap() += TimeDelta::seconds(delay);
            *self.tick.lock().unwrap() += delay as f64;
            if let Some(message) = self.failure.lock().unwrap().clone() {
                return Err(SessionError::new(503, "effect_failed", message));
            }
            Ok(TuneReceipt::new(check))
        })
    }
}
impl SessionEffects for Effects {
    fn tune(&self, _: TuneRequest) -> SessionFuture<'_, TuneReceipt> {
        self.tune_impl(Arc::new(Check))
    }
    fn validate_reference(&self, _: Value, _: Value) -> SessionFuture<'_, ()> {
        Box::pin(async { Ok(()) })
    }
}
impl AutomationEffects for Effects {
    fn capture(&self, _: AutomationScope) -> Result<Arc<dyn TuneCompletionCheck>, SessionError> {
        Ok(Arc::new(Check))
    }
    fn detect(
        &self,
        request: DetectionRequest,
        check: Arc<dyn TuneCompletionCheck>,
        _: Cancellation,
    ) -> SessionFuture<'_, Vec<DetectedRegion>> {
        Box::pin(async move {
            check.revalidate()?;
            // Scientific values come from actual Python authority vectors. The
            // facade must not reimplement sweep algorithms or apply calibration.
            let source = fixture();
            let vector = source["regions"]
                .as_array()
                .unwrap()
                .iter()
                .find(|v| v["sweep"]["levels"] == request.sweep["levels"])
                .unwrap();
            assert_eq!(
                request.threshold_dbm,
                vector["threshold_dbm"].as_f64().unwrap()
            );
            Ok(serde_json::from_value(vector["regions"].clone()).unwrap())
        })
    }
    fn tune(
        &self,
        _: TuneRequest,
        check: Arc<dyn TuneCompletionCheck>,
        _: Cancellation,
    ) -> SessionFuture<'_, TuneReceipt> {
        self.tune_impl(check)
    }
}
fn fixture() -> Value {
    serde_json::from_str(include_str!(
        "../../../contracts/fixtures/automation-domain-v1.json"
    ))
    .unwrap()
}
fn kind(value: &str) -> PolicyKind {
    match value {
        "detection" => PolicyKind::Detection,
        "handoff" => PolicyKind::Handoff,
        "completion" => PolicyKind::Completion,
        _ => panic!("unknown fixture kind"),
    }
}

#[tokio::test]
async fn actual_owned_python_sqlite_policy_status_events_and_errors_match() {
    for case in fixture()["cases"].as_array().unwrap() {
        let directory = tempfile::tempdir().unwrap();
        let archive =
            Archive::open(ArchiveConfig::new(directory.path().join("archive.sqlite"))).unwrap();
        for session in case["seed"]["sessions"].as_array().unwrap() {
            archive.create_session(session.clone()).await.unwrap();
        }
        let now = Arc::new(Mutex::new("2026-11-11T12:00:00.123456Z".parse().unwrap()));
        let tick = Arc::new(Mutex::new(0.0));
        let effects = Arc::new(Effects {
            now: now.clone(),
            tick: tick.clone(),
            failure: Mutex::new(None),
            delay: Mutex::new(0),
        });
        let clock = now.clone();
        let ids = Arc::new(AtomicUsize::new(0));
        let session = SessionService::with_sources(
            archive.clone(),
            effects.clone(),
            Arc::new(move || *clock.lock().unwrap()),
            Arc::new(move || format!("generated-{:04}", ids.fetch_add(1, Ordering::SeqCst) + 1)),
        );
        let make = || {
            let clock = now.clone();
            let mono = tick.clone();
            AutomationService::with_sources(
                archive.clone(),
                session.clone(),
                effects.clone(),
                AutomationConfig {
                    start_background: false,
                    ..AutomationConfig::default()
                },
                Arc::new(move || *clock.lock().unwrap()),
                Arc::new(move || *mono.lock().unwrap()),
            )
            .unwrap()
        };
        let mut automation = make();
        let sid = case["seed"]["sessions"][0]["id"].as_str().unwrap();
        for (index, action) in case["actions"].as_array().unwrap().iter().enumerate() {
            *now.lock().unwrap() = action["at"].as_str().unwrap().parse().unwrap();
            *tick.lock().unwrap() = action["monotonic"].as_f64().unwrap();
            *effects.failure.lock().unwrap() = action["effect"]
                .as_str()
                .filter(|v| *v != "ok")
                .map(str::to_owned);
            *effects.delay.lock().unwrap() = action["tune_delay_seconds"].as_i64().unwrap();
            let latest = archive.latest_events(1).await.unwrap();
            let before = latest.last().map_or(0, |v| v["sequence"].as_u64().unwrap());
            let args = action["args"].as_array().unwrap();
            let result = if action["method"] == "restart" {
                automation.close().await.unwrap();
                automation = make();
                let mut statuses = serde_json::Map::new();
                for name in ["detection", "handoff", "completion"] {
                    statuses.insert(
                        name.into(),
                        automation.status(kind(name), sid).await.unwrap(),
                    );
                }
                Ok(Value::Object(statuses))
            } else if action["kind"] == "domain" {
                match action["method"].as_str().unwrap() {
                    "ingest_sweep" => Ok(archive
                        .append_sweeps(vec![args[0].clone()])
                        .await
                        .unwrap()
                        .items
                        .remove(0)),
                    "ingest_observation" => Ok(archive
                        .append_observations(vec![args[0].clone()])
                        .await
                        .unwrap()
                        .items
                        .remove(0)),
                    "create_candidate" => session.create_candidate(sid, args[1].clone()).await,
                    "enqueue_investigation" => {
                        session
                            .enqueue_investigation(sid, args[1].as_str().unwrap(), args[2].clone())
                            .await
                    }
                    "stop_session" => {
                        let mut value = archive.get_session(sid).await.unwrap().unwrap();
                        value["state"] = json!("stopped");
                        value["ended_at"] = action["at"].clone();
                        archive
                            .stop_session(value, action["at"].as_str().unwrap())
                            .await
                            .map(|mut b| b.items.remove(0))
                            .map_err(Into::into)
                    }
                    other => panic!("unhandled domain action {other}"),
                }
            } else {
                let policy = kind(action["kind"].as_str().unwrap());
                match action["method"].as_str().unwrap() {
                    "configure" => automation.configure(policy, sid, args[0].clone()).await,
                    "status" => automation.status(policy, sid).await,
                    "run_once" => automation.run_once(policy, sid).await,
                    other => panic!("unhandled policy action {other}"),
                }
            };
            let label = format!(
                "{} action{index} {} {}",
                case["name"], action["kind"], action["method"]
            );
            if let Some(error) = action.get("error") {
                let actual = result.expect_err(&label);
                assert_eq!(
                    json!({"http_status":actual.http_status,"api_code":actual.api_code,"message":actual.message}),
                    *error,
                    "{label}"
                );
            } else {
                assert_eq!(result.unwrap(), action["result"], "{label}");
            }
            let mut events = archive.events(before, 1000).await.unwrap();
            let mut expected_events = action["events"].as_array().unwrap().clone();
            for event in &mut events {
                event["sequence"] = json!(event["sequence"].as_u64().unwrap() - before);
            }
            if action["kind"] == "domain"
                && ["ingest_sweep", "ingest_observation"]
                    .contains(&action["method"].as_str().unwrap())
            {
                // Archive acquisition timestamps use its real UTC clock, while
                // Session/automation clocks are injectable. Assert validity and
                // compare the complete committed acquisition payload/sequence.
                for event in &mut events {
                    event["occurred_at"]
                        .as_str()
                        .unwrap()
                        .parse::<DateTime<Utc>>()
                        .unwrap();
                    event.as_object_mut().unwrap().remove("occurred_at");
                }
                for event in &mut expected_events {
                    event.as_object_mut().unwrap().remove("occurred_at");
                }
            }
            assert_eq!(events, expected_events, "events {label}");
        }
        automation.close().await.unwrap();
        archive.close().await.unwrap();
    }
}
