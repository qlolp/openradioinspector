use chrono::{DateTime, Utc};
use ori_tasks::{
    ControlTask, ReferenceBook, TaskContext, TaskEffects, TaskError, TaskFuture, TaskScheduler,
    TaskSchedulerConfig,
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::sync::{Arc, Mutex};

struct Effects {
    calls: Arc<Mutex<Vec<Value>>>,
    failures: Vec<Value>,
}
impl Effects {
    fn callback(&self, phase: &str, context: TaskContext) -> Result<(), TaskError> {
        let task = context.task.as_dict();
        self.calls
            .lock()
            .unwrap()
            .push(json!({"phase":phase,"task":task}));
        if let Some(failure) = self
            .failures
            .iter()
            .find(|value| value["phase"] == phase && value["name"] == task["name"])
        {
            return Err(TaskError::new(
                503,
                "fixture_effect_failed",
                failure["message"].as_str().unwrap(),
            ));
        }
        Ok(())
    }
}
impl TaskEffects for Effects {
    fn on_start(&self, context: TaskContext) -> TaskFuture<'_, ()> {
        Box::pin(async move { self.callback("start", context) })
    }
    fn on_complete(&self, context: TaskContext) -> TaskFuture<'_, ()> {
        Box::pin(async move { self.callback("complete", context) })
    }
}
fn fixture() -> Value {
    let before = include_bytes!("../../../contracts/fixtures/task-scheduler-before-v1.json");
    assert_eq!(
        format!("{:x}", Sha256::digest(before)),
        "cd792b1ae4deb85f51e50c2195721c2e7705a29884f7becacf40f2b83f957a27",
        "preserved actual Python before-golden"
    );
    let raw = include_bytes!("../../../contracts/fixtures/task-scheduler-v1.json");
    assert_eq!(
        format!("{:x}", Sha256::digest(raw)),
        "be55970ed205e2dfbdc6e81bb6141f3e0f7a3a43ed5789ba8aa37cf078c43ee6"
    );
    serde_json::from_slice(raw).unwrap()
}
fn paths(value: &Value, root: &str, scrub: bool) -> Value {
    match value {
        Value::String(value) => json!(if scrub {
            value.replace(root, "$TMP")
        } else {
            value.replace("$TMP", root)
        }),
        Value::Array(values) => values.iter().map(|v| paths(v, root, scrub)).collect(),
        Value::Object(values) => Value::Object(
            values
                .iter()
                .map(|(k, v)| (k.clone(), paths(v, root, scrub)))
                .collect(),
        ),
        _ => value.clone(),
    }
}
fn exact(actual: &Value, expected: &Value, context: &str) {
    assert_eq!(actual, expected, "{context}");
    match (actual, expected) {
        (Value::Number(a), Value::Number(b)) if b.to_string().contains(['.', 'e', 'E']) => {
            assert_eq!(
                a.as_f64().unwrap().to_bits(),
                b.as_f64().unwrap().to_bits(),
                "{context}: f64 bits"
            );
        }
        (Value::Array(a), Value::Array(b)) => {
            for (a, b) in a.iter().zip(b) {
                exact(a, b, context);
            }
        }
        (Value::Object(a), Value::Object(b)) => {
            for (k, b) in b {
                exact(&a[k], b, context);
            }
        }
        _ => {}
    }
}
fn disk(path: &std::path::Path) -> Value {
    if path.exists() {
        serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap()
    } else {
        Value::Null
    }
}

#[tokio::test]
async fn actual_python_taskbook_oracle_preserves_dto_book_errors_order_and_effects() {
    for case in fixture()["cases"].as_array().unwrap() {
        let directory = tempfile::tempdir().unwrap();
        let root = directory.path().to_str().unwrap();
        let path = directory.path().join("existing-book.json");
        std::fs::write(directory.path().join("scan-данные.json"), "{}\n").unwrap();
        if !case["seed"].is_null() {
            std::fs::write(
                &path,
                serde_json::to_vec(&paths(&case["seed"], root, false)).unwrap(),
            )
            .unwrap();
        }
        let calls = Arc::new(Mutex::new(Vec::new()));
        let effects = Arc::new(Effects {
            calls: calls.clone(),
            failures: case["failures"].as_array().unwrap().clone(),
        });
        let mut config = TaskSchedulerConfig::new(&path);
        config.interval_seconds = 60.;
        let service = TaskScheduler::with_clock(
            config,
            effects,
            Arc::new(|| {
                "2026-11-11T12:00:00.123456Z"
                    .parse::<DateTime<Utc>>()
                    .unwrap()
            }),
        )
        .unwrap();
        exact(
            &service.status().await.unwrap(),
            &case["initial"]["status"],
            "initial status",
        );
        exact(
            &paths(&disk(&path), root, true),
            &case["initial"]["disk"],
            "initial recovery disk",
        );
        for (index, action) in case["actions"].as_array().unwrap().iter().enumerate() {
            let args = paths(&action["args"], root, false);
            let method = action["method"].as_str().unwrap();
            let result = match method {
                "create_task" => service.create_task(args[0].clone()).await,
                "cancel_task" => service.cancel_task(args[0].as_str().unwrap()).await,
                "list_tasks" => service.list_tasks().await,
                "configure" => service.configure(args[0].clone()).await,
                "run_once" => {
                    service
                        .run_once(Some(action["at"].as_str().unwrap().parse().unwrap()))
                        .await
                }
                "close" => service.close().await.map(|()| Value::Null),
                "status" => service.status().await,
                _ => panic!("uncovered action{method}"),
            };
            let context = format!("{} #{index} {method}", case["name"]);
            if let Some(expected) = action.get("error") {
                let error = result.expect_err(&context);
                assert_eq!(error.message, expected["message"], "{context}");
                if !expected["http_status"].is_null() {
                    assert_eq!(
                        json!(error.http_status),
                        expected["http_status"],
                        "{context}"
                    );
                    assert_eq!(error.api_code, expected["api_code"], "{context}");
                }
            } else {
                exact(
                    &paths(
                        &result.unwrap_or_else(|e| panic!("{context}: {e}")),
                        root,
                        true,
                    ),
                    &action["result"],
                    &context,
                );
            }
            exact(
                &service.status().await.unwrap(),
                &action["status"],
                &context,
            );
            exact(&paths(&disk(&path), root, true), &action["disk"], &context);
            exact(
                &paths(&json!(*calls.lock().unwrap()), root, true),
                &action["calls"],
                &context,
            );
        }
        service.close().await.unwrap();
    }
}

#[test]
fn reference_book_reassigns_ids_and_preserves_user_unicode_and_custom_kinds() {
    let value = &fixture()["reference_book"];
    let mut book = ReferenceBook::from_dict(value["input"].clone()).unwrap();
    assert_eq!(
        json!(book.add("purpose", json!("third"), json!("x"))),
        value["added"]
    );
    assert_eq!(book.as_dict(), value["book"]);
    assert_eq!(json!(book.list("purpose")), value["list"]);
    assert_eq!(json!(book.get("purpose", 2)), value["found"]);
    assert!(book.get("absent", 1).is_none());
    assert_eq!(book.name_of("purpose", Some(0)), value["empty_name"]);
    assert_eq!(book.name_of("purpose", Some(1)), value["name"]);
    assert_ne!(book.list("purpose")[0].name, book.list("purpose")[1].name);
}

#[test]
fn full_control_task_roundtrip_uses_existing_fields_without_unicode_normalization() {
    let value = fixture()["cases"][2]["seed"]["tasks"][0].clone();
    let task = ControlTask::from_dict(value.clone()).unwrap();
    exact(&task.as_dict(), &value, "full task roundtrip");
}
