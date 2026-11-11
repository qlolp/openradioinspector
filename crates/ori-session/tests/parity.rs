use chrono::{DateTime, Utc};
use ori_archive::{Archive, ArchiveConfig};
use ori_session::{
    SessionEffects, SessionError, SessionFuture, SessionService, TuneCompletionCheck, TuneReceipt,
    TuneRequest,
};
use serde_json::{Value, json};
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicUsize, Ordering},
};

struct Receipt;
impl TuneCompletionCheck for Receipt {
    fn revalidate(&self) -> Result<(), SessionError> {
        Ok(())
    }
}
#[derive(Default)]
struct Effects {
    failure: Mutex<Option<String>>,
    calls: Mutex<Vec<TuneRequest>>,
}
impl SessionEffects for Effects {
    fn tune(&self, request: TuneRequest) -> SessionFuture<'_, TuneReceipt> {
        Box::pin(async move {
            self.calls.lock().unwrap().push(request);
            if let Some(message) = self.failure.lock().unwrap().clone() {
                return Err(SessionError {
                    http_status: 503,
                    api_code: "effect_failed".into(),
                    message,
                });
            }
            Ok(TuneReceipt::new(Arc::new(Receipt)))
        })
    }
    fn validate_reference(&self, _session: Value, _sweep: Value) -> SessionFuture<'_, ()> {
        // The business fixture contains compatible self-comparisons. Scientific
        // compatibility is independently delegated/tested by Core's real pool.
        Box::pin(async { Ok(()) })
    }
}

fn fixture() -> Value {
    serde_json::from_str(include_str!(
        "../../../contracts/fixtures/session-domain-v1.json"
    ))
    .unwrap()
}
fn string(value: &Value) -> &str {
    value.as_str().unwrap()
}

#[tokio::test]
async fn actual_python_sqlite_golden_lifecycle_catalog_reference_and_signal_list_parity() {
    for case in fixture()["cases"].as_array().unwrap() {
        let directory = tempfile::tempdir().unwrap();
        let archive =
            Archive::open(ArchiveConfig::new(directory.path().join("archive.sqlite"))).unwrap();
        for session in case["seed"]["sessions"].as_array().unwrap() {
            archive.create_session(session.clone()).await.unwrap();
        }
        archive
            .append_sweeps(case["seed"]["sweeps"].as_array().unwrap().clone())
            .await
            .unwrap();
        archive
            .append_observations(case["seed"]["observations"].as_array().unwrap().clone())
            .await
            .unwrap();
        let effects = Arc::new(Effects::default());
        let now = Arc::new(Mutex::new(
            "2026-11-11T12:00:00.123456Z"
                .parse::<DateTime<Utc>>()
                .unwrap(),
        ));
        let clock = now.clone();
        let ids = Arc::new(AtomicUsize::new(0));
        let generated = ids.clone();
        let service = SessionService::with_sources(
            archive.clone(),
            effects.clone(),
            Arc::new(move || *clock.lock().unwrap()),
            Arc::new(move || {
                format!(
                    "generated-{:04}",
                    generated.fetch_add(1, Ordering::SeqCst) + 1
                )
            }),
        );
        for action in case["actions"].as_array().unwrap() {
            *now.lock().unwrap() = string(&action["at"]).parse().unwrap();
            *effects.failure.lock().unwrap() = action
                .get("effect")
                .and_then(Value::as_str)
                .filter(|effect| *effect != "ok")
                .map(str::to_owned);
            let last = archive.latest_events(1).await.unwrap();
            let before = last
                .last()
                .map_or(0, |event| event["sequence"].as_u64().unwrap());
            let args = action["args"].as_array().unwrap();
            let method = string(&action["method"]);
            let result = match method {
                "create_candidate" => {
                    service
                        .create_candidate(string(&args[0]), args[1].clone())
                        .await
                }
                "get_candidate" => {
                    service
                        .get_candidate(string(&args[0]), string(&args[1]))
                        .await
                }
                "list_candidates" => service.list_candidates(string(&args[0]), None).await,
                "dismiss_candidate" => {
                    service
                        .dismiss_candidate(string(&args[0]), string(&args[1]))
                        .await
                }
                "enqueue_investigation" => {
                    service
                        .enqueue_investigation(string(&args[0]), string(&args[1]), args[2].clone())
                        .await
                }
                "list_investigations" => service.list_investigations(string(&args[0]), None).await,
                "next_investigation" => service.next_investigation(string(&args[0])).await,
                "start_investigation" => {
                    service
                        .start_investigation(string(&args[0]), string(&args[1]))
                        .await
                }
                "start_next_investigation" => {
                    service
                        .start_next_investigation(string(&args[0]), string(&args[1]))
                        .await
                }
                "matching_observation_ids" => {
                    service
                        .matching_observation_ids(string(&args[0]), string(&args[1]), None)
                        .await
                }
                "complete_investigation" => {
                    service
                        .complete_investigation(string(&args[0]), string(&args[1]))
                        .await
                }
                "fail_investigation" => {
                    service
                        .fail_investigation(string(&args[0]), string(&args[1]), string(&args[2]))
                        .await
                }
                "cancel_investigation" => {
                    service
                        .cancel_investigation(string(&args[0]), string(&args[1]))
                        .await
                }
                "create_sweep_reference" => {
                    service
                        .create_sweep_reference(string(&args[0]), args[1].clone())
                        .await
                }
                "list_sweep_references" => {
                    service.list_sweep_references(string(&args[0]), None).await
                }
                "measurement_catalog" => service.measurement_catalog().await,
                "save_measurement_result" => {
                    service
                        .save_measurement_result(string(&args[0]), args[1].clone())
                        .await
                }
                "list_measurement_results" => {
                    service
                        .list_measurement_results(string(&args[0]), None, Some(100))
                        .await
                }
                "signal_list_export" => {
                    service
                        .signal_list_export(string(&args[0]), string(&args[1]), None)
                        .await
                }
                "signal_list_import" => {
                    service
                        .signal_list_import(string(&args[0]), args[1].clone())
                        .await
                }
                _ => panic!("uncovered fixture action {method}"),
            };
            if let Some(expected) = action.get("error") {
                let error = result.expect_err(&format!("{} {method}", case["name"]));
                assert_eq!(
                    json!({"http_status":error.http_status,"api_code":error.api_code,"message":error.message}),
                    *expected,
                    "{} {method}",
                    case["name"]
                );
            } else {
                assert_eq!(
                    result.unwrap_or_else(|error| panic!(
                        "{} {method}: {}",
                        case["name"], error.message
                    )),
                    action["result"],
                    "{} {method}",
                    case["name"]
                );
            }
            let mut events = archive.events(before, 1000).await.unwrap();
            for event in &mut events {
                event["sequence"] = json!(event["sequence"].as_u64().unwrap() - before);
            }
            assert_eq!(
                json!(events),
                action["events"],
                "{} {method} events",
                case["name"]
            );
        }
        archive.close().await.unwrap();
    }
}
