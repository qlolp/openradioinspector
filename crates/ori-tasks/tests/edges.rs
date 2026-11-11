use chrono::{DateTime, TimeDelta, Utc};
use ori_tasks::{ControlTask, NoopTaskEffects, TaskScheduler, TaskSchedulerConfig};
use serde_json::{Value, json};
use std::sync::Arc;
fn fixture() -> Value {
    serde_json::from_str(include_str!(
        "../../../contracts/fixtures/task-edges-v1.json"
    ))
    .unwrap()
}

#[tokio::test]
async fn actual_python_accepted_iso_forms_and_rejection_messages_are_preserved() {
    let dir = tempfile::tempdir().unwrap();
    let input = dir.path().join("input");
    std::fs::write(&input, "{}").unwrap();
    let service = TaskScheduler::open(
        TaskSchedulerConfig::new(dir.path().join("tasks.json")),
        Arc::new(NoopTaskEffects),
    )
    .unwrap();
    for (index, value) in fixture()["times"].as_array().unwrap().iter().enumerate() {
        let result=service.create_task(json!({"name":format!("time-{index}"),"task_file":input,"start_at":value["input"],"duration_s":1})).await;
        if let Some(error) = value.get("error") {
            assert_eq!(result.unwrap_err().message, *error, "{}", value["input"]);
        } else {
            assert_eq!(
                result.unwrap_or_else(|error| panic!("{}: {error}", value["input"])),
                value["result"],
                "{}",
                value["input"]
            );
        }
    }
    service.close().await.unwrap();
}
#[test]
fn actual_python_legacy_numeric_and_iterable_coercions_preserve_full_fields() {
    for value in fixture()["coercions"].as_array().unwrap() {
        let task = ControlTask::from_dict(value["input"].clone())
            .unwrap_or_else(|error| panic!("{}: {error}", value["input"]));
        assert_eq!(task.as_dict(), value["result"], "{}", value["input"]);
    }
}
#[tokio::test]
async fn scheduled_end_uses_python_microsecond_rounding_and_overflow_outcomes() {
    let base = "2026-11-11T12:00:00.123456Z"
        .parse::<DateTime<Utc>>()
        .unwrap();
    for value in fixture()["durations"].as_array().unwrap() {
        let dir = tempfile::tempdir().unwrap();
        let input = dir.path().join("input");
        std::fs::write(&input, "{}").unwrap();
        let mut config = TaskSchedulerConfig::new(dir.path().join("tasks.json"));
        config.interval_seconds = 60.;
        let service = TaskScheduler::open(config, Arc::new(NoopTaskEffects)).unwrap();
        service.create_task(json!({"name":"duration","task_file":input,"start_at":"2026-11-11T12:00:00.123456Z","duration_s":value["duration_s"]})).await.unwrap();
        service.configure(json!({"enabled":true})).await.unwrap();
        service.run_once(Some(base)).await.unwrap();
        if let Some(error) = value.get("error") {
            service
                .run_once(Some(base + TimeDelta::seconds(1)))
                .await
                .unwrap();
            let task = &service.list_tasks().await.unwrap()[0];
            assert_eq!(task["status"], "Снято с выполнения");
            assert_eq!(task["outcome"], *error, "duration{}", value["duration_s"]);
        } else {
            let ends = value["ends_at"]
                .as_str()
                .unwrap()
                .parse::<DateTime<Utc>>()
                .unwrap();
            service
                .run_once(Some(ends - TimeDelta::microseconds(1)))
                .await
                .unwrap();
            assert_eq!(
                service.list_tasks().await.unwrap()[0]["status"],
                "Выполняется",
                "one microsecond before{}",
                value["duration_s"]
            );
            service.run_once(Some(ends)).await.unwrap();
            assert_eq!(
                service.list_tasks().await.unwrap()[0]["status"],
                "Выполнено",
                "at exact scheduled end{}",
                value["duration_s"]
            );
        }
        service.close().await.unwrap();
    }
}
